"""High-level translation API used by the HTTP routes and by document jobs (SPEC §5).

Never logs source or translated text (counts and timings only).
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from collections import Counter, OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from typing import Any, Literal, TypeVar

from translator_app.config import Settings
from translator_app.engines.base import TranslationEngine
from translator_app.languages import unit_joiner
from translator_app.llm.client import (
    RETRY_FINISH_REASONS,
    LLMClient,
    LLMError,
    LLMInputTooLong,
    LLMOutputError,
    TranslationCancelled,
    VisionUnavailable,
    estimate_tokens,
)
from translator_app.llm.prompts import TranslationSpec
from translator_app.schemas import (
    AlternativesRequest,
    DetectedLanguage,
    GlossaryEntry,
    LookupEntry,
    LookupExample,
    LookupRequest,
    LookupResponse,
    RewriteRequest,
    RewriteResponse,
    TranslateOptions,
    TranslationResponse,
    TranslationSegment,
)
from translator_app.services.glossary import GlossaryStore, match_glossary_entries, normalize_language_code
from translator_app.services.language_detection import (
    DetectionSummary,
    detect_source_languages,
    resolve_source_language,
)
from translator_app.services.segmentation import TextUnit, join_units, normalize_line_breaks, split_units

logger = logging.getLogger("translator.service")

T = TypeVar("T")
Priority = Literal["interactive", "document"]

BATCH_TOKENS = 2000          # ≈ source tokens per structured-output request
BATCH_ITEMS = 30             # items per request
LONG_ITEM_TOKENS = 2500      # untagged items above this are translated in sentence chunks
MAX_ALTERNATIVES = 5
IMAGE_GLOSSARY_LIMIT = 40

MSG_GLOSSARY_NOT_FOUND = "선택한 용어집을 찾을 수 없습니다."

_TAG_RE = re.compile(r"<(/?)([gx]\d+)(/?)>")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_URL_OR_EMAIL_RE = re.compile(r"(?:https?://|ftp://|www\.)\S+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_\-./:#()]*$")


class GlossaryNotFound(LookupError):
    def __init__(self, message: str = MSG_GLOSSARY_NOT_FOUND) -> None:
        super().__init__(message)


class _OwnerGone(Exception):
    """The request that was producing a shared (deduplicated) unit stopped; waiters retry themselves."""


# ---------------------------------------------------------------- helpers
def tag_signature(text: str) -> Counter[str]:
    return Counter(match.group(0) for match in _TAG_RE.finditer(text))


def _tag_distance(a: Counter[str], b: Counter[str]) -> int:
    return sum((a - b).values()) + sum((b - a).values())


def plain_text(text: str) -> str:
    """Text without inline tags and entities."""
    return html.unescape(_TAG_RE.sub(" ", text)) if _TAG_RE.search(text) or "&" in text else text


def needs_translation(text: str) -> bool:
    """False for blank items and items made only of numbers / punctuation / URLs / e-mails / codes."""
    plain = plain_text(text).strip()
    if not plain:
        return False
    rest = _URL_OR_EMAIL_RE.sub(" ", plain)
    if not _LETTER_RE.search(rest):
        return False
    tokens = rest.split()
    if len(tokens) == 1 and _CODE_RE.match(tokens[0]) and any(ch.isdigit() for ch in tokens[0]):
        return False
    return True


def _keep_outer_whitespace(source: str, translated: str) -> str:
    lead = source[: len(source) - len(source.lstrip())]
    trail = source[len(source.rstrip()):]
    return f"{lead}{translated.strip()}{trail}"


def _fail_future(future: asyncio.Future[Any], exc: BaseException) -> None:
    if not future.done():
        future.set_exception(exc)
        future.exception()  # mark as retrieved: waiters may not exist


async def _gather_all(aws: Iterable[Awaitable[T]]) -> list[T]:
    """gather() that cancels the remaining tasks when one fails (or when we are cancelled)."""
    tasks = [asyncio.ensure_future(aw) for aw in aws]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _with_cancel(aw: Awaitable[T], cancel: asyncio.Event | None) -> T:
    """Await ``aw``; if ``cancel`` is set first, cancel it (closing upstream requests) and raise."""
    if cancel is None:
        return await aw
    if cancel.is_set():
        if asyncio.iscoroutine(aw):
            aw.close()
        raise TranslationCancelled()
    work = asyncio.ensure_future(aw)
    stopper = asyncio.ensure_future(cancel.wait())
    try:
        await asyncio.wait({work, stopper}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        work.cancel()
        stopper.cancel()
        await asyncio.gather(work, stopper, return_exceptions=True)
        raise
    if work.done():
        stopper.cancel()
        await asyncio.gather(stopper, return_exceptions=True)
        return work.result()
    work.cancel()
    await asyncio.gather(work, return_exceptions=True)
    raise TranslationCancelled()


@dataclass(slots=True)
class _Prepared:
    text: str
    units: list[TextUnit]
    detection: DetectionSummary
    source_lang: str
    identity: bool
    hits: list[GlossaryEntry]
    glossary_name: str | None
    spec: TranslationSpec
    detected: list[DetectedLanguage] = field(default_factory=list)

    def unit_spec(self, unit_text: str) -> TranslationSpec:
        if not self.hits:
            return self.spec
        matched = match_glossary_entries(unit_text, self.hits, target_lang=self.spec.target_lang,
                                         source_lang=self.source_lang)
        return replace(self.spec, glossary=tuple(matched))


# ---------------------------------------------------------------- service
class TranslatorService:
    def __init__(
        self,
        settings: Settings,
        llm: LLMClient | None,
        engine: TranslationEngine,
        glossary_store: GlossaryStore,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.engine = engine
        self.glossary_store = glossary_store
        self._cache: OrderedDict[tuple[Any, ...], str] = OrderedDict()
        self._inflight: dict[tuple[Any, ...], asyncio.Future[str]] = {}

    @property
    def engine_name(self) -> str:
        return self.engine.name

    # ------------------------------------------------------------------ glossary
    async def _glossary_source(self, opts: TranslateOptions) -> tuple[list[GlossaryEntry], str | None]:
        if not opts.use_glossary:
            return [], None
        entries = opts.glossary_entries
        name: str | None = None
        if opts.glossary_id:
            document = await asyncio.to_thread(self._load_glossary, opts.glossary_id)
            if document is None:
                raise GlossaryNotFound()
            name = document.name
            if entries is None:
                entries = document.entries
        return list(entries or []), name

    def _load_glossary(self, glossary_id: str):  # noqa: ANN202 - GlossaryDocument | None
        try:
            return self.glossary_store.load_glossary(glossary_id)
        except ValueError:  # invalid id
            return None

    async def resolve_glossary(self, opts: TranslateOptions, text: str) -> tuple[list[GlossaryEntry], str | None]:
        """(entries whose source term occurs in ``text`` for this direction, glossary name)."""
        entries, name = await self._glossary_source(opts)
        if not entries:
            return [], name
        matched = match_glossary_entries(text, entries, target_lang=opts.target_lang, source_lang=opts.source_lang)
        return matched, name

    # ------------------------------------------------------------------ cache + in-flight dedupe
    def _cache_get(self, key: tuple[Any, ...]) -> str | None:
        value = self._cache.get(key)
        if value is not None:
            self._cache.move_to_end(key)
        return value

    def _cache_put(self, key: tuple[Any, ...], value: str) -> None:
        limit = max(0, self.settings.segment_cache_size)
        if limit == 0:
            return
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > limit:
            self._cache.popitem(last=False)

    async def _shared(self, key: tuple[Any, ...], produce: Callable[[], Awaitable[str]]) -> tuple[str, bool]:
        """Result for ``key`` from the cache, from an identical request already running, or by
        running ``produce``. Returns (text, produced_here)."""
        while True:
            cached = self._cache_get(key)
            if cached is not None:
                return cached, False
            future = self._inflight.get(key)
            if future is None:
                break
            try:
                return await asyncio.shield(future), False
            except _OwnerGone:
                continue
        future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            result = await produce()
        except asyncio.CancelledError:
            _fail_future(future, _OwnerGone())
            raise
        except BaseException as exc:
            _fail_future(future, exc)
            raise
        else:
            self._cache_put(key, result)
            future.set_result(result)
            return result, True
        finally:
            if self._inflight.get(key) is future:
                del self._inflight[key]

    @staticmethod
    def _unit_key(text: str, spec: TranslationSpec) -> tuple[Any, ...]:
        return ("unit", spec.cache_key(), text)

    async def _translate_unit_cached(self, text: str, spec: TranslationSpec, priority: Priority) -> str:
        async def produce() -> str:
            return (await self.engine.translate_unit(text, spec, priority=priority)).strip()

        result, _ = await self._shared(self._unit_key(text, spec), produce)
        return result

    # ------------------------------------------------------------------ preparation
    async def _prepare(self, text: str, opts: TranslateOptions) -> _Prepared:
        normalized = normalize_line_breaks(text)
        detection = detect_source_languages(normalized)
        source_lang = resolve_source_language(opts.source_lang, detection)
        if source_lang != "auto":
            source_lang = normalize_language_code(source_lang)
        identity = source_lang != "auto" and source_lang == opts.target_lang
        entries, glossary_name = await self._glossary_source(opts)
        hits: list[GlossaryEntry] = []
        if entries and not identity:
            hits = match_glossary_entries(normalized, entries, target_lang=opts.target_lang, source_lang=source_lang)
        spec = TranslationSpec(
            source_lang=source_lang,
            target_lang=opts.target_lang,
            formality=opts.formality,
            context=opts.context,
            instructions=opts.instructions,
        )
        detected = [
            DetectedLanguage(code=item.code, char_count=item.char_count, share=item.share)
            for item in detection.languages
        ]
        return _Prepared(
            text=normalized,
            units=split_units(normalized, max_chars=self.settings.text_chunk_chars),
            detection=detection,
            source_lang=source_lang,
            identity=identity,
            hits=hits,
            glossary_name=glossary_name,
            spec=spec,
            detected=detected,
        )

    @staticmethod
    def _detected_lang(prep: _Prepared) -> str | None:
        if prep.source_lang != "auto":
            return prep.source_lang
        return prep.detection.primary_language

    # ------------------------------------------------------------------ text: one shot
    async def translate_text(self, text: str, opts: TranslateOptions) -> TranslationResponse:
        started = time.perf_counter()
        prep = await self._prepare(text, opts)
        if prep.identity or not prep.units:
            targets = [unit.text for unit in prep.units]
            translation = prep.text if prep.identity else ""
        else:
            targets = await _gather_all(
                self._translate_unit_cached(unit.text, prep.unit_spec(unit.text), "interactive")
                for unit in prep.units
            )
            translation = join_units(prep.units, targets, joiner=unit_joiner(opts.target_lang))
        return TranslationResponse(
            translation=translation,
            segments=[TranslationSegment(source=u.text, target=t) for u, t in zip(prep.units, targets, strict=True)],
            glossary_hits=prep.hits,
            glossary_applied=bool(prep.hits),
            glossary_name=prep.glossary_name,
            detected_source_languages=prep.detected,
            primary_source_lang=prep.detection.primary_language,
            source_language_mode=prep.detection.mode,
            detected_source_lang=self._detected_lang(prep),
            formality=opts.formality,
            engine=self.engine.name,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    # ------------------------------------------------------------------ text: streaming
    async def stream_text(self, text: str, opts: TranslateOptions) -> AsyncIterator[dict[str, Any]]:
        """NDJSON events (SPEC §7). Closing the generator cancels every upstream request."""
        started = time.perf_counter()
        try:
            prep = await self._prepare(text, opts)
        except GlossaryNotFound as exc:
            yield {"type": "error", "detail": str(exc)}
            return
        units = prep.units
        yield {
            "type": "start",
            "units": len(units),
            "segments": [unit.text for unit in units],
            "detected_source_languages": [item.model_dump() for item in prep.detected],
            "primary_source_lang": prep.detection.primary_language,
            "source_language_mode": prep.detection.mode,
            "detected_source_lang": self._detected_lang(prep),
            "glossary_hits": [entry.model_dump() for entry in prep.hits],
            "glossary_name": prep.glossary_name,
        }
        results: list[str | None] = [None] * len(units)

        def done_event() -> dict[str, Any]:
            targets = [value or "" for value in results]
            return {
                "type": "done",
                "translation": prep.text if prep.identity else join_units(
                    units, targets, joiner=unit_joiner(opts.target_lang)),
                "segments": [{"source": u.text, "target": t} for u, t in zip(units, targets, strict=True)],
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "engine": self.engine.name,
            }

        if prep.identity:
            for index, unit in enumerate(units):
                results[index] = unit.text
                yield {"type": "delta", "index": index, "text": unit.text}
                yield {"type": "unit", "index": index, "text": unit.text}
            yield done_event()
            return

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        pending: list[int] = []
        for index, unit in enumerate(units):
            cached = self._cache_get(self._unit_key(unit.text, prep.unit_spec(unit.text)))
            if cached is None:
                pending.append(index)
                continue
            results[index] = cached
            yield {"type": "delta", "index": index, "text": cached}
            yield {"type": "unit", "index": index, "text": cached}

        tasks = [
            asyncio.create_task(self._stream_unit_task(index, units[index].text, prep.unit_spec(units[index].text),
                                                       queue))
            for index in pending
        ]
        try:
            remaining = len(tasks)
            while remaining:
                event = await queue.get()
                kind = event["type"]
                if kind == "_failed":
                    raise event["error"]
                if kind == "unit":
                    remaining -= 1
                    results[event["index"]] = event["text"]
                yield event
            yield done_event()
        except LLMError as exc:
            logger.warning("스트리밍 번역 실패: %s", exc)
            yield {"type": "error", "detail": str(exc)}
        except Exception:  # noqa: BLE001 - the client must always get an error line
            logger.exception("스트리밍 번역 중 오류")
            yield {"type": "error", "detail": "번역 중 오류가 발생했습니다."}
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                try:
                    await asyncio.wait(tasks, timeout=5)
                except BaseException:  # noqa: BLE001 - cancelled while cleaning up: tasks still finish
                    pass

    async def _stream_unit_task(self, index: int, text: str, spec: TranslationSpec,
                                queue: asyncio.Queue[dict[str, Any]]) -> None:
        async def produce() -> str:
            pieces: list[str] = []
            meta: dict[str, Any] = {}
            async with aclosing(self.engine.stream_unit(text, spec, priority="interactive", meta=meta)) as stream:
                async for piece in stream:
                    pieces.append(piece)
                    queue.put_nowait({"type": "delta", "index": index, "text": piece})
            result = "".join(pieces).strip()
            if meta.get("finish_reason") in RETRY_FINISH_REASONS or not result:
                # looped / truncated / empty stream: one non-stream retry (the unit event replaces the text)
                result = (await self.engine.translate_unit(text, spec, priority="interactive", retry=True)).strip()
            return result

        try:
            result, produced = await self._shared(self._unit_key(text, spec), produce)
            if not produced:
                queue.put_nowait({"type": "delta", "index": index, "text": result})
            queue.put_nowait({"type": "unit", "index": index, "text": result})
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - forwarded to the consumer
            queue.put_nowait({"type": "_failed", "index": index, "error": exc})

    # ------------------------------------------------------------------ documents: batches
    async def translate_batch(
        self,
        texts: list[str],
        opts: TranslateOptions,
        *,
        priority: Priority = "document",
        preceding: list[tuple[str, str]] | None = None,
        tags: bool = False,
        cancel: asyncio.Event | None = None,
    ) -> list[str]:
        """Translate many independent items; same length and order as ``texts``."""
        if cancel is not None and cancel.is_set():
            raise TranslationCancelled()
        out = list(texts)
        source_lang = opts.source_lang or "auto"
        if source_lang != "auto":
            source_lang = normalize_language_code(source_lang)
        if source_lang != "auto" and source_lang == opts.target_lang:
            return out
        entries, _ = await self._glossary_source(opts)
        if entries:
            entries = match_glossary_entries("\n".join(texts), entries, target_lang=opts.target_lang,
                                             source_lang=source_lang)
        exact = {entry.source.strip().casefold(): entry.target for entry in entries}
        base_spec = TranslationSpec(
            source_lang=source_lang,
            target_lang=opts.target_lang,
            formality=opts.formality,
            context=opts.context,
            instructions=opts.instructions,
        )

        todo: list[int] = []
        for index, text in enumerate(texts):
            if not text or not text.strip():
                continue
            if exact and not _TAG_RE.search(text):
                hit = exact.get(text.strip().casefold())
                if hit is not None:
                    out[index] = _keep_outer_whitespace(text, hit)
                    continue
            if needs_translation(text):
                todo.append(index)
        if not todo:
            return out

        batches: list[list[int]] = []
        current: list[int] = []
        tokens = 0
        for index in todo:
            size = estimate_tokens(texts[index])
            if current and (tokens + size > BATCH_TOKENS or len(current) >= BATCH_ITEMS):
                batches.append(current)
                current, tokens = [], 0
            current.append(index)
            tokens += size
        if current:
            batches.append(current)

        gate = asyncio.Semaphore(max(1, self.settings.llm_doc_parallel if priority == "document"
                                     else self.settings.llm_max_parallel))
        started = time.perf_counter()

        async def run(batch_no: int, batch: list[int]) -> None:
            if batch_no == 0:
                context = preceding
            else:
                context = [(texts[i], "") for i in batches[batch_no - 1][-3:]]
            items = [texts[i] for i in batch]
            async with gate:
                if cancel is not None and cancel.is_set():
                    raise TranslationCancelled()
                spec = self._batch_spec(base_spec, entries, items, source_lang)
                results = await self._translate_group(items, spec, priority=priority, tags=tags,
                                                      preceding=context, cancel=cancel)
            for i, value in zip(batch, results, strict=True):
                out[i] = value

        await _gather_all(run(number, batch) for number, batch in enumerate(batches))
        logger.info("일괄 번역 %d건 (%d회 묶음) %.1f초", len(todo), len(batches), time.perf_counter() - started)
        return out

    @staticmethod
    def _batch_spec(base: TranslationSpec, entries: list[GlossaryEntry], items: Sequence[str],
                    source_lang: str) -> TranslationSpec:
        if not entries:
            return base
        matched = match_glossary_entries("\n".join(plain_text(item) for item in items), entries,
                                         target_lang=base.target_lang, source_lang=source_lang)
        return replace(base, glossary=tuple(matched))

    async def _translate_group(
        self,
        items: list[str],
        spec: TranslationSpec,
        *,
        priority: Priority,
        tags: bool,
        preceding: Sequence[tuple[str, str]] | None,
        cancel: asyncio.Event | None,
    ) -> list[str]:
        """One structured request; on a bad answer split in halves, finally one item at a time."""
        if len(items) == 1:
            return [await self._translate_single(items[0], spec, priority=priority, tags=tags, cancel=cancel)]
        try:
            outputs = await _with_cancel(
                self.engine.translate_items(items, spec, priority=priority, tags=tags, preceding=preceding), cancel)
        except (LLMOutputError, LLMInputTooLong) as exc:
            logger.info("묶음 번역 응답 오류 (%d건): %s — 나눠서 다시 요청합니다", len(items), exc)
            half = len(items) // 2
            left = await self._translate_group(items[:half], self._batch_spec(spec, list(spec.glossary),
                                                                              items[:half], spec.source_lang),
                                               priority=priority, tags=tags, preceding=preceding, cancel=cancel)
            right = await self._translate_group(items[half:], self._batch_spec(spec, list(spec.glossary),
                                                                               items[half:], spec.source_lang),
                                                priority=priority, tags=tags, preceding=None, cancel=cancel)
            return left + right
        results: list[str] = []
        for source, output in zip(items, outputs, strict=True):
            results.append(await self._check_item(source, output, spec, priority=priority, tags=tags, cancel=cancel))
        return results

    async def _translate_single(self, text: str, spec: TranslationSpec, *, priority: Priority, tags: bool,
                                cancel: asyncio.Event | None) -> str:
        if not tags and not _TAG_RE.search(text) and estimate_tokens(text) > LONG_ITEM_TOKENS:
            units = split_units(text, max_chars=max(400, self.settings.text_chunk_chars))
            parts = []
            for unit in units:
                part = await _with_cancel(self.engine.translate_unit(unit.text, spec, priority=priority), cancel)
                parts.append(part.strip() or unit.text)
            return _keep_outer_whitespace(text, join_units(units, parts, joiner=unit_joiner(spec.target_lang)))
        output = await _with_cancel(self.engine.translate_unit(text, spec, priority=priority, tags=tags), cancel)
        return await self._check_item(text, output, spec, priority=priority, tags=tags, cancel=cancel, retried=False)

    async def _check_item(self, source: str, output: str, spec: TranslationSpec, *, priority: Priority, tags: bool,
                          cancel: asyncio.Event | None, retried: bool = False) -> str:
        """Empty output → one plain retry; broken tags → one strict retry at temperature 0;
        otherwise the best output as-is (the documents layer decides the fallback)."""
        if not output.strip():
            if retried:
                return source
            output = await _with_cancel(self.engine.translate_unit(source, spec, priority=priority, tags=tags,
                                                                   retry=True), cancel)
            if not output.strip():
                return source
        if tags:
            expected = tag_signature(source)
            got = tag_signature(output)
            if expected != got:
                tag_list = " ".join(sorted(expected)) or "(none)"
                retry = await _with_cancel(
                    self.engine.translate_unit(source, spec, priority=priority, tags=True, strict_tags=tag_list),
                    cancel)
                if retry.strip() and _tag_distance(expected, tag_signature(retry)) < _tag_distance(expected, got):
                    output = retry
        return _keep_outer_whitespace(source, output)

    # ------------------------------------------------------------------ images
    async def describe_image(
        self,
        image_bytes: bytes,
        mime: str,
        opts: TranslateOptions,
        *,
        mode: Literal["translate", "extract"] = "translate",
        priority: Priority = "document",
    ) -> str:
        if self.llm is not None and not self.llm.vision_enabled:
            raise VisionUnavailable()
        entries: list[GlossaryEntry] = []
        if mode == "translate":
            all_entries, _ = await self._glossary_source(opts)
            target = normalize_language_code(opts.target_lang)
            entries = [entry for entry in all_entries
                       if entry.enabled and normalize_language_code(entry.target_lang) == target][:IMAGE_GLOSSARY_LIMIT]
        spec = TranslationSpec(
            source_lang=opts.source_lang or "auto",
            target_lang=opts.target_lang,
            formality=opts.formality,
            context=opts.context,
            instructions=opts.instructions,
            glossary=tuple(entries),
        )
        result = await self.engine.describe_image(image_bytes, mime, spec, mode=mode, priority=priority)
        return result.strip()

    # ------------------------------------------------------------------ alternatives / rewrite / lookup
    async def alternatives(self, req: AlternativesRequest) -> list[str]:
        span = req.span.strip()
        if not span:
            return []
        source_lang = req.source_lang or "auto"
        if source_lang == "auto" and req.source.strip():
            source_lang = resolve_source_language("auto", detect_source_languages(req.source))
        hits, _ = await self.resolve_glossary(req.model_copy(update={"source_lang": source_lang}),
                                              req.source or req.translation)
        spec = TranslationSpec(
            source_lang=source_lang,
            target_lang=req.target_lang,
            formality=req.formality,
            context=req.context,
            instructions=req.instructions,
            glossary=tuple(hits),
        )
        raw = await self.engine.alternatives(req.source, req.translation, span, spec)
        seen = {span.casefold()}
        result: list[str] = []
        for item in raw:
            value = str(item).strip()
            if value and value.casefold() not in seen:
                seen.add(value.casefold())
                result.append(value)
        return result[:MAX_ALTERNATIVES]

    async def rewrite(self, req: RewriteRequest) -> RewriteResponse:
        text = normalize_line_breaks(req.text)
        lang: str | None = req.lang if req.lang and req.lang != "auto" else None
        if lang is None and text.strip():
            detection = detect_source_languages(text)
            lang = detection.primary_language or (detection.languages[0].code if detection.languages else None)
        if not text.strip():
            return RewriteResponse(text="", detected_lang=lang)
        result = await self.engine.rewrite(text, lang, req.style, req.context)
        return RewriteResponse(text=result.strip() or text, detected_lang=lang)

    async def lookup(self, req: LookupRequest) -> LookupResponse:
        term = req.term.strip()
        source_lang = req.source_lang or "auto"
        if source_lang == "auto":
            source_lang = resolve_source_language("auto", detect_source_languages(f"{term} {req.context}".strip()))
        value = await self.engine.lookup(term, req.context, source_lang, req.target_lang)
        entries: list[LookupEntry] = []
        for item in value.get("entries") or []:
            if isinstance(item, dict) and str(item.get("translation", "")).strip():
                entries.append(LookupEntry(translation=str(item["translation"]).strip(),
                                           pos=str(item.get("pos") or "").strip(),
                                           note=str(item.get("note") or "").strip()))
        examples: list[LookupExample] = []
        for item in value.get("examples") or []:
            if isinstance(item, dict) and str(item.get("source", "")).strip() and str(item.get("target", "")).strip():
                examples.append(LookupExample(source=str(item["source"]).strip(), target=str(item["target"]).strip()))
        return LookupResponse(term=term, entries=entries, examples=examples)

    # ------------------------------------------------------------------ status
    async def model_status(self) -> dict[str, Any]:
        if self.llm is None:
            return {"connected": True, "name": self.engine.name, "max_model_len": None, "vision": True,
                    "running": None, "waiting": None, "error": None}
        return await self.llm.status()
