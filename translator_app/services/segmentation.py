from __future__ import annotations

import re
from dataclasses import dataclass


_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?。！？])\s+|\n+")
_PARAGRAPH_BOUNDARY = re.compile(r"\n{2}")
_TOO_MANY_BREAKS = re.compile(r"\n{3,}")
_LINE_BREAKS = re.compile(r"\r\n?|\n")


def normalize_line_breaks(text: str) -> str:
    normalized = _LINE_BREAKS.sub("\n", text)
    return _TOO_MANY_BREAKS.sub("\n\n", normalized)


def split_text(text: str) -> list[str]:
    stripped = normalize_line_breaks(text).strip()
    if not stripped:
        return []

    parts = [part.strip() for part in _SENTENCE_BOUNDARY.split(stripped)]
    return [part for part in parts if part]


def split_translation_units(text: str, *, max_chars: int) -> list[str]:
    normalized = normalize_line_breaks(text).strip()
    if not normalized:
        return []

    paragraphs = [paragraph.strip() for paragraph in _PARAGRAPH_BOUNDARY.split(normalized)]
    paragraphs = [paragraph for paragraph in paragraphs if paragraph]

    units: list[str] = []
    current: list[str] = []
    current_len = 0

    for paragraph in paragraphs:
        for piece in _split_long_paragraph(paragraph, max_chars=max_chars):
            joiner_len = 2 if current else 0
            next_len = current_len + joiner_len + len(piece)
            if current and next_len > max_chars:
                units.append("\n\n".join(current))
                current = [piece]
                current_len = len(piece)
            else:
                current.append(piece)
                current_len = next_len if current_len else len(piece)

    if current:
        units.append("\n\n".join(current))

    return units


def _split_long_paragraph(paragraph: str, *, max_chars: int) -> list[str]:
    if len(paragraph) <= max_chars:
        return [paragraph]

    sentences = split_text(paragraph)
    if len(sentences) <= 1:
        return [paragraph[index : index + max_chars] for index in range(0, len(paragraph), max_chars)]

    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = sentence if not current else f"{current} {sentence}"
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = candidate

    if current:
        chunks.append(current)

    return chunks


# ---------------------------------------------------------------- interactive units (v2)
_PARAGRAPH_SPLIT = re.compile(r"\n[ \t]*\n")
# Latin terminators need whitespace after them ("e.g. 3.5"); full-width CJK terminators end a sentence
# by themselves (Chinese/Japanese put no space after 。！？), also when a closing bracket/quote follows.
_SENTENCE_END = re.compile(
    r"(?<=[。！？])(?![」』）)”’〕】》。！？])\s*"
    r"|(?<=[。！？][」』）)”’〕】》])\s*"
    r"|(?<=[.!?…])\s+"
    r"|\n"
)
_SOFT_BREAK = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class TextUnit:
    """One translation unit: an exact substring of the normalized source text."""

    text: str
    paragraph: int
    newline_before: bool = False  # chunk of a long paragraph that started on a new line


def split_units(text: str, *, max_chars: int) -> list[TextUnit]:
    """Paragraphs (split on blank lines); paragraphs longer than ``max_chars`` are split
    at sentence ends / line breaks into chunks of at most ``max_chars``."""
    normalized = normalize_line_breaks(text)
    units: list[TextUnit] = []
    paragraph_index = 0
    for raw in _PARAGRAPH_SPLIT.split(normalized):
        paragraph = raw.strip()
        if not paragraph:
            continue
        if len(paragraph) <= max_chars:
            units.append(TextUnit(paragraph, paragraph_index))
        else:
            for chunk, newline in _chunk_paragraph(paragraph, max_chars):
                units.append(TextUnit(chunk, paragraph_index, newline))
        paragraph_index += 1
    return units


def join_units(units: list[TextUnit], translations: list[str], *, joiner: str = " ") -> str:
    """Rebuild the full text: paragraphs separated by a blank line, chunks of one
    paragraph by a line break (if the source had one) or ``joiner``."""
    parts: list[str] = []
    previous: TextUnit | None = None
    for unit, translated in zip(units, translations, strict=True):
        if previous is not None:
            if unit.paragraph != previous.paragraph:
                parts.append("\n\n")
            else:
                parts.append("\n" if unit.newline_before else joiner)
        parts.append(translated.strip())
        previous = unit
    return "".join(parts)


def _chunk_paragraph(paragraph: str, max_chars: int) -> list[tuple[str, bool]]:
    # (start, end, newline_before) spans of sentences
    spans: list[tuple[int, int, bool]] = []
    start = 0
    newline = False
    for match in _SENTENCE_END.finditer(paragraph):
        if match.start() > start:
            spans.append((start, match.start(), newline))
        newline = "\n" in match.group(0)
        start = match.end()
    if start < len(paragraph):
        spans.append((start, len(paragraph), newline))

    # split sentences that are still too long at whitespace (hard cut as a last resort)
    pieces: list[tuple[int, int, bool]] = []
    for s, e, nl in spans:
        while e - s > max_chars:
            cut = s + max_chars
            soft = [m.start() for m in _SOFT_BREAK.finditer(paragraph, s + max_chars // 2, cut)]
            if soft:
                cut = soft[-1]
            pieces.append((s, cut, nl))
            nl = False
            s = cut
            while s < e and paragraph[s].isspace():
                nl = nl or paragraph[s] == "\n"
                s += 1
        if s < e:
            pieces.append((s, e, nl))

    # greedily merge consecutive pieces up to max_chars (merged chunks keep the original gap text)
    chunks: list[tuple[str, bool]] = []
    cur_start: int | None = None
    cur_end = 0
    cur_newline = False
    for s, e, nl in pieces:
        if cur_start is not None and e - cur_start <= max_chars:
            cur_end = e
            continue
        if cur_start is not None:
            chunks.append((paragraph[cur_start:cur_end].strip(), cur_newline))
        cur_start, cur_end, cur_newline = s, e, nl
    if cur_start is not None:
        chunks.append((paragraph[cur_start:cur_end].strip(), cur_newline))
    return [(chunk, nl) for chunk, nl in chunks if chunk]
