"""A very small Markdown reader for model output (image OCR / scanned pages).

Only what a vision model typically produces: headings, paragraphs, bullet and
numbered lists, block quotes, pipe tables, fenced code, horizontal rules and
**bold** / *italic* / `code` inline marks.  Rendered to DOCX (docx_writer) and
to HTML for PDF pages (pdf).
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+•]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d+[.)])\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_INLINE = re.compile(r"(\*\*[^*\n]+?\*\*|__[^_\n]+?__|`[^`\n]+`|(?<![\w*])\*[^*\n]+?\*(?![\w*]))")


@dataclass
class Block:
    kind: str                       # heading | para | bullet | numbered | quote | table | code | hr
    text: str = ""
    level: int = 0
    rows: list[list[str]] = field(default_factory=list)


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]


def parse(md: str) -> list[Block]:
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[Block] = []
    para: list[str] = []
    i = 0

    def flush():
        if para:
            blocks.append(Block("para", "\n".join(para)))
            para.clear()

    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            flush()
            i += 1
            continue
        if _FENCE.match(ln):
            flush()
            tok = _FENCE.match(ln).group(1)
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(tok):
                body.append(lines[i])
                i += 1
            blocks.append(Block("code", "\n".join(body)))
            i += 1
            continue
        if ln.strip().startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush()
            rows = [_cells(ln)]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            blocks.append(Block("table", rows=rows))
            continue
        m = _HEADING.match(ln)
        if m:
            flush()
            blocks.append(Block("heading", m.group(2), level=len(m.group(1))))
            i += 1
            continue
        if _HR.match(ln):
            flush()
            blocks.append(Block("hr"))
            i += 1
            continue
        m = _BULLET.match(ln)
        if m:
            flush()
            blocks.append(Block("bullet", m.group(2), level=len(m.group(1).expandtabs(4)) // 2))
            i += 1
            continue
        m = _NUMBERED.match(ln)
        if m:
            flush()
            blocks.append(Block("numbered", f"{m.group(2)} {m.group(3)}", level=len(m.group(1).expandtabs(4)) // 2))
            i += 1
            continue
        m = _QUOTE.match(ln)
        if m:
            flush()
            blocks.append(Block("quote", m.group(1)))
            i += 1
            continue
        para.append(ln.strip())
        i += 1
    flush()
    return blocks


def inline_runs(text: str) -> list[tuple[str, str]]:
    """-> [(text, style)] with style in "", "b", "i", "code"."""
    out: list[tuple[str, str]] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], ""))
        tok = m.group(0)
        if tok.startswith(("**", "__")):
            out.append((tok[2:-2], "b"))
        elif tok.startswith("`"):
            out.append((tok[1:-1], "code"))
        else:
            out.append((tok[1:-1], "i"))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], ""))
    return out


def plain(text: str) -> str:
    return "".join(t for t, _ in inline_runs(text))


def _inline_html(text: str) -> str:
    parts = []
    for t, st in inline_runs(text):
        e = html.escape(t).replace("\n", "<br/>")
        if st == "b":
            e = f"<b>{e}</b>"
        elif st == "i":
            e = f"<i>{e}</i>"
        elif st == "code":
            e = f"<code>{e}</code>"
        parts.append(e)
    return "".join(parts)


def to_html(blocks: list[Block]) -> str:
    out: list[str] = []
    for b in blocks:
        if b.kind == "heading":
            lv = min(max(b.level, 1), 4)
            out.append(f"<h{lv}>{_inline_html(b.text)}</h{lv}>")
        elif b.kind == "para":
            out.append(f"<p>{_inline_html(b.text)}</p>")
        elif b.kind in ("bullet", "numbered"):
            mark = "•" if b.kind == "bullet" else ""
            pad = 12 + 12 * b.level
            out.append(f'<p style="margin-left:{pad}pt">{mark + " " if mark else ""}{_inline_html(b.text)}</p>')
        elif b.kind == "quote":
            out.append(f'<p style="margin-left:12pt;color:#555555">{_inline_html(b.text)}</p>')
        elif b.kind == "code":
            out.append(f"<pre>{html.escape(b.text)}</pre>")
        elif b.kind == "hr":
            out.append("<hr/>")
        elif b.kind == "table" and b.rows:
            rows = []
            for r, cells in enumerate(b.rows):
                tag = "th" if r == 0 else "td"
                rows.append("<tr>" + "".join(f"<{tag}>{_inline_html(c)}</{tag}>" for c in cells) + "</tr>")
            out.append("<table>" + "".join(rows) + "</table>")
    return "\n".join(out)
