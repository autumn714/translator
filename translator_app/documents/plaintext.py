"""TXT / Markdown / SRT / WebVTT translation that keeps the file structure."""
from __future__ import annotations

import re
from pathlib import Path

from translator_app.documents.base import HandlerContext, Translate, TwoPassHandler, has_letters
from translator_app.documents.inline_tags import translate_unique


# ------------------------------------------------------------- encoding
def decode_text(data: bytes) -> tuple[str, str]:
    """-> (text, bom) where bom is "\\ufeff" when the output should carry one."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "\ufeff"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace"), "\ufeff"
    try:
        return data.decode("utf-8"), ""
    except UnicodeDecodeError:
        pass
    try:
        # Korean Windows (ANSI = CP949): write UTF-8 with a BOM so Notepad/Excel detect it
        return data.decode("cp949"), "\ufeff"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "\ufeff"


def encode_text(text: str, bom: str) -> bytes:
    return (bom + text).encode("utf-8")


# ------------------------------------------------------------- placeholders
_PH = re.compile(r"<x(\d+)/>")


def protect(s: str, patterns: list[re.Pattern]) -> tuple[str, list[str]]:
    """Replace protected spans (inline code, URLs, tags) by <xN/> placeholders."""
    keep: list[str] = []

    def sub(m):
        keep.append(m.group(0))
        return f"<x{len(keep)}/>"
    # one combined pass: a later pattern must never re-match an earlier placeholder
    rx = re.compile("|".join(f"(?:{p.pattern})" for p in patterns), re.M)
    return rx.sub(sub, s), keep


def restore(s: str, keep: list[str]) -> str:
    used = set()

    def sub(m):
        i = int(m.group(1))
        if 1 <= i <= len(keep):
            used.add(i)
            return keep[i - 1]
        return ""
    s = _PH.sub(sub, s)
    missing = [k for i, k in enumerate(keep, 1) if i not in used]
    return s + ("" if not missing else " " + " ".join(missing))   # never lose code/URLs


def _newline(src: str) -> str:
    return "\r\n" if "\r\n" in src else "\n"


# ------------------------------------------------------------- TXT
def translate_text(src: str, translate: Translate, *, bilingual: bool = False) -> str:
    """Line by line; indentation, blank lines and line endings preserved.
    bilingual: the translated line follows each source line."""
    nl = _newline(src)
    lines = src.splitlines(keepends=True)
    jobs = []
    for i, ln in enumerate(lines):
        body = ln.rstrip("\r\n")
        if has_letters(body):
            jobs.append((i, body))
    table = translate_unique([b.strip() for _, b in jobs], translate)
    for i, body in jobs:
        lead = body[:len(body) - len(body.lstrip())]
        trail = body[len(body.rstrip()):]
        eol = lines[i][len(body):]
        new = lead + table[body.strip()] + trail
        if bilingual:
            lines[i] = body + (eol or nl) + new + eol
        else:
            lines[i] = new + eol
    return "".join(lines)


# ------------------------------------------------------------- Markdown
MD_PREFIX = re.compile(r"^(\s*(?:>\s*)*(?:#{1,6}\s+|[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)?)(.*?)(\s*#*\s*)$")
MD_PROTECT = [
    re.compile(r"`+[^`]*`+"),                               # inline code
    re.compile(r"(?<=\])\([^)]*\)"),                       # (url "title") of links/images
    re.compile(r"<https?://[^>]+>|https?://\S+"),           # bare / auto links
    re.compile(r"</?[A-Za-z][^>]*>"),                       # inline HTML
    re.compile(r"\[\^?[^\]]*\]:\s*\S+.*$"),               # reference definitions
]
FENCE = re.compile(r"^\s*(```|~~~)")
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def translate_markdown(src: str, translate: Translate, *, bilingual: bool = False) -> str:
    nl = _newline(src)
    lines = src.splitlines(keepends=True)
    in_fence, fence_tok, front = False, "", False
    slots = []                  # (line idx, cell idx or None, prefix, protected text, keep, suffix, cells)
    for i, raw in enumerate(lines):
        ln = raw.rstrip("\r\n")
        if i == 0 and ln.strip() == "---":
            front = True
            continue
        if front:
            if ln.strip() in ("---", "..."):
                front = False
            continue
        m = FENCE.match(ln)
        if m:
            if not in_fence:
                in_fence, fence_tok = True, m.group(1)
            elif m.group(1) == fence_tok:
                in_fence = False
            continue
        if in_fence or not has_letters(ln) or TABLE_SEP.match(ln) or ln.lstrip().startswith("<!--"):
            continue
        if "|" in ln and ln.strip().startswith("|"):          # table row: cell by cell
            cells = re.split(r"(?<!\\)\|", ln)
            for c, cell in enumerate(cells):
                if has_letters(cell):
                    p, keep = protect(cell.strip(), MD_PROTECT)
                    lead = cell[:len(cell) - len(cell.lstrip())]
                    trail = cell[len(cell.rstrip()):]
                    slots.append((i, c, lead, p, keep, trail, cells))
            continue
        m = MD_PREFIX.match(ln)
        prefix, body, suffix = m.group(1), m.group(2), m.group(3)
        if not has_letters(body):
            continue
        p, keep = protect(body, MD_PROTECT)
        slots.append((i, None, prefix, p, keep, suffix, None))
    table = translate_unique([s[3] for s in slots], translate)
    rows: dict = {}
    new_lines: dict[int, str] = {}
    for i, c, pre, p, keep, suf, cells in slots:
        out = restore(table[p], keep)
        if c is None:
            new_lines[i] = pre + out + suf
        elif bilingual:
            # a second row would break the table (header must precede the separator row)
            rows.setdefault(i, list(cells))[c] = pre + cells[c].strip() + "<br>" + out + suf
        else:
            rows.setdefault(i, list(cells))[c] = pre + out + suf
    for i, cells in rows.items():
        new_lines[i] = "|".join(cells)
    for i, new in new_lines.items():
        body = lines[i].rstrip("\r\n")
        eol = lines[i][len(body):]
        if bilingual:
            if i in rows:
                lines[i] = new + eol                                # source<br>translation per cell
            else:
                # blank line between: the copy becomes its own block (heading, list item, paragraph)
                lines[i] = body + (eol or nl) + (eol or nl) + new + eol
        else:
            lines[i] = new + eol
    return "".join(lines)


# ------------------------------------------------------------- SRT / WebVTT
TIMING = re.compile(r"-->")
SUB_PROTECT = [re.compile(r"</?[A-Za-z][^>]*>"), re.compile(r"\{\\[^}]*\}")]   # <i>, <font>, {\an8}


def translate_subtitles(src: str, translate: Translate) -> str:
    """SRT and WebVTT: index/timing/header/NOTE/STYLE lines untouched; the text lines
    of one cue are translated together (joined by \\n) to keep the sentence."""
    nl = _newline(src)
    blocks = re.split(r"(?:\r?\n){2,}", src.strip("\ufeff\r\n"))
    jobs = []
    for bi, b in enumerate(blocks):
        ls = b.splitlines()
        if not ls or ls[0].startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        t = next((k for k, line in enumerate(ls) if TIMING.search(line)), None)
        if t is None or t + 1 >= len(ls):
            continue
        text = "\n".join(ls[t + 1:])
        if has_letters(text):
            p, keep = protect(text, SUB_PROTECT)
            jobs.append((bi, t, p, keep))
    table = translate_unique([j[2] for j in jobs], translate)
    for bi, t, p, keep in jobs:
        ls = blocks[bi].splitlines()
        blocks[bi] = nl.join(ls[:t + 1] + restore(table[p], keep).split("\n"))
    bom = "\ufeff" if src.startswith("\ufeff") else ""
    return bom + (nl + nl).join(blocks) + nl


# ------------------------------------------------------------- handler
class PlainTextHandler(TwoPassHandler):
    """.txt .md .markdown .srt .vtt"""

    def __init__(self) -> None:
        self._cache: tuple[str, str] | None = None

    def _source(self, src: Path) -> tuple[str, str]:
        if self._cache is None:
            self._cache = decode_text(src.read_bytes())
        return self._cache

    def _run(self, text: str, translate: Translate, ctx: HandlerContext) -> str:
        bilingual = ctx.output_mode == "bilingual"
        if ctx.ext in (".md", ".markdown"):
            return translate_markdown(text, translate, bilingual=bilingual)
        if ctx.ext in (".srt", ".vtt"):
            return translate_subtitles(text, translate)
        return translate_text(text, translate, bilingual=bilingual)

    def process(self, src: Path, dst: Path | None, translate: Translate, ctx: HandlerContext) -> None:
        text, bom = self._source(src)
        out = self._run(text, translate, ctx)
        if dst is not None:
            dst.write_bytes(encode_text(out, bom))
            self._output = out

    def preview(self, ctx: HandlerContext, pairs: list[tuple[str, str]]) -> str:
        out = getattr(self, "_output", "")
        return out.lstrip("\ufeff")[:20_000]
