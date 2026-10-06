"""Shared types for document translation handlers.

Every handler follows the same shape: CPU-heavy parsing runs in a worker thread,
the strings to translate are collected first (pass 1), translated asynchronously
in document order by the job manager, and the output is written in a second
pass that looks the translations up (pass 2).  Recipes therefore only ever see a
synchronous ``translate(list[str]) -> list[str]`` callable.
"""
from __future__ import annotations

import asyncio
import html
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Protocol

Translate = Callable[[list[str]], list[str]]
OutputMode = Literal["translated", "bilingual"]
PdfMode = Literal["layout", "docx"]
JobStatus = Literal["queued", "extracting", "translating", "writing", "done", "error", "canceled"]


class DocumentError(Exception):
    """Error with a concise Korean message that is shown to the user as-is."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# ------------------------------------------------------------------ messages
MSG_LLM_UNAVAILABLE = "모델 서버에 연결할 수 없습니다. 도면 분석기 모델 서버가 켜져 있는지 확인하세요."
MSG_VISION_UNAVAILABLE = "이미지 인식을 지원하지 않는 모델입니다."
MSG_INTERRUPTED = "서버가 다시 시작되어 작업이 중단되었습니다."
MSG_BROKEN = "파일을 열 수 없습니다. 암호·DRM(문서 보안)이 걸려 있거나 손상된 파일입니다."
MSG_ENCRYPTED = "암호가 걸린 문서입니다. 암호를 해제한 뒤 다시 올려 주세요."
MSG_HWP_AS_DOCX = (
    "HWP 파일은 서식을 유지할 수 없어 Word 파일로 저장했습니다. "
    "한글에서 HWPX로 저장해 올리면 서식이 유지됩니다."
)
MSG_FAILED = "문서를 처리하지 못했습니다. 파일이 손상되었거나 지원하지 않는 구조일 수 있습니다."
MSG_NO_TEXT = "번역할 텍스트가 없습니다."


# ------------------------------------------------------------------ tag helpers
TAG_RE = re.compile(r"<(/?)([gx]\d+)(/?)>")
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def has_letters(s: str) -> bool:
    return bool(_LETTER.search(s))


def has_tags(s: str) -> bool:
    return bool(TAG_RE.search(s))


def strip_tags(s: str) -> str:
    """Remove <gN>/<xN/> tags and unescape the entities added by the tag encoder."""
    if not TAG_RE.search(s):
        return s
    return html.unescape(TAG_RE.sub("", s))


def tag_signature(s: str) -> Counter:
    return Counter(m.group(0) for m in TAG_RE.finditer(s))


def tags_ok(src: str, out: str) -> bool:
    """True when the translation kept every tag of the source exactly once and the
    <xN/> atoms in their original order (what the recipes' decoders accept)."""
    if not TAG_RE.search(src):
        return True
    if tag_signature(src) != tag_signature(out):
        return False

    def atoms(s: str) -> list[str]:
        return [m.group(2) for m in TAG_RE.finditer(s) if m.group(2).startswith("x")]

    return atoms(src) == atoms(out)


_URLISH = re.compile(r"^(?:[a-z][a-z0-9+.-]*://\S+|www\.\S+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+)$", re.I)


def needs_translation(s: str) -> bool:
    """Blank / numbers / punctuation / single URL or e-mail -> keep as is."""
    plain = strip_tags(s).strip()
    if not plain or not has_letters(plain):
        return False
    return not _URLISH.match(plain)


# ------------------------------------------------------------------ two-pass glue
class Collector:
    """Pass-1 translate: records the requested strings (unique, in order) and
    returns them unchanged so the recipe can run to completion."""

    def __init__(self) -> None:
        self.items: list[str] = []
        self._seen: set[str] = set()

    def __call__(self, texts: list[str]) -> list[str]:
        for t in texts:
            if t not in self._seen:
                self._seen.add(t)
                self.items.append(t)
        return list(texts)


class Lookup:
    """Pass-2 translate: answers from the translation table."""

    def __init__(self, table: dict[str, str]) -> None:
        self.table = table

    def __call__(self, texts: list[str]) -> list[str]:
        return [self.table.get(t, t) for t in texts]


# ------------------------------------------------------------------ handler protocol
@dataclass
class HandlerResult:
    preview: str = ""                                   # translated text (≤ 20k chars kept)
    pairs: list[tuple[str, str]] = field(default_factory=list)   # (source, target) units for the report
    output_ext: str | None = None                       # None = same as the input extension


@dataclass
class HandlerContext:
    """What a handler may use.  Built by the JobManager for one job."""

    input_path: Path
    output_path: Path                                   # final path (extension decided by the handler)
    ext: str                                            # input extension, lower case, with dot
    opts: Any                                           # TranslateOptions (without document-only fields)
    output_mode: OutputMode
    pdf_mode: PdfMode
    target_lang: str
    font_dir: Path | None
    translate_strings: Callable[[list[str]], Awaitable[dict[str, str]]]
    describe_image: Callable[[bytes, str], Awaitable[str]]
    set_status: Callable[[JobStatus], None]
    add_units: Callable[[int, int], None]               # (extra units, extra chars) for non-text work (images)
    unit_done: Callable[[int, int], None]               # (units, chars) finished outside translate_strings
    warn: Callable[[str], None]
    cancel: asyncio.Event
    max_chars: int = 600_000

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            from translator_app.llm.client import TranslationCancelled

            raise TranslationCancelled()

    def output_with_ext(self, ext: str) -> Path:
        return self.output_path.with_suffix(ext)


class DocumentHandler(Protocol):
    output_ext: str | None

    async def run(self, ctx: HandlerContext) -> HandlerResult: ...


class TwoPassHandler:
    """Runs a synchronous recipe twice: collect (dst=None) then write (dst=output)."""

    output_ext: str | None = None

    def validate(self, ctx: HandlerContext) -> None:
        """Cheap structural checks before pass 1 (raise DocumentError)."""

    def process(self, src: Path, dst: Path | None, translate: Translate, ctx: HandlerContext) -> None:
        raise NotImplementedError

    def preview(self, ctx: HandlerContext, pairs: list[tuple[str, str]]) -> str:
        return preview_from_pairs(pairs)

    def output_path(self, ctx: HandlerContext) -> Path:
        return ctx.output_with_ext(self.output_ext) if self.output_ext else ctx.output_path

    async def run(self, ctx: HandlerContext) -> HandlerResult:
        ctx.set_status("extracting")
        await asyncio.to_thread(self.validate, ctx)
        collector = Collector()
        await asyncio.to_thread(self.process, ctx.input_path, None, collector, ctx)
        ctx.check_cancel()
        if not collector.items:
            raise DocumentError(MSG_NO_TEXT)
        table = await ctx.translate_strings(collector.items)
        ctx.check_cancel()
        ctx.set_status("writing")
        await asyncio.to_thread(self.process, ctx.input_path, self.output_path(ctx), Lookup(table), ctx)
        pairs = [(s, table.get(s, s)) for s in collector.items]
        return HandlerResult(preview=self.preview(ctx, pairs), pairs=pairs, output_ext=self.output_ext)


def preview_from_pairs(pairs: list[tuple[str, str]], limit: int = 20_000) -> str:
    out: list[str] = []
    n = 0
    for _, tgt in pairs:
        t = strip_tags(tgt).strip()
        if not t:
            continue
        out.append(t)
        n += len(t) + 1
        if n >= limit:
            break
    return "\n".join(out)[:limit]
