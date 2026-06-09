from __future__ import annotations

import re


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
