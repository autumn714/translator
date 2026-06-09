from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
from typing import Literal

from translator_app.languages import LANGUAGE_MAP

try:
    from langdetect import DetectorFactory, detect_langs

    DetectorFactory.seed = 0
except Exception:  # pragma: no cover - fallback path when dependency is unavailable.
    detect_langs = None


LanguageMode = Literal["single", "mixed", "unknown"]

_UNIT_PATTERN = re.compile(r"\n{2,}|(?<=[.!?。！？])\s+")


@dataclass(slots=True)
class DetectedLanguageStat:
    code: str
    char_count: int
    share: float


@dataclass(slots=True)
class DetectionSummary:
    mode: LanguageMode
    primary_language: str | None
    languages: list[DetectedLanguageStat]


def resolve_source_language(requested_source_lang: str, detection: DetectionSummary) -> str:
    normalized = requested_source_lang.strip() or "auto"
    if normalized != "auto":
        return normalized
    if detection.mode == "single" and detection.primary_language:
        return detection.primary_language
    return "auto"


def detect_source_languages(text: str) -> DetectionSummary:
    normalized = text.strip()
    if not normalized:
        return DetectionSummary(mode="unknown", primary_language=None, languages=[])

    summary = _detect_with_langdetect(normalized)
    if summary is not None:
        return summary
    return _detect_with_script_fallback(normalized)


def _detect_with_langdetect(text: str) -> DetectionSummary | None:
    if detect_langs is None:
        return None

    counts = Counter[str]()
    direct_evidence = Counter[str]()
    han_count = 0.0

    for unit in _iter_detection_units(text):
        direct_counts, unit_han_count, detectable_text, detectable_length = _split_detection_unit(unit)
        counts.update(direct_counts)
        direct_evidence.update(direct_counts)
        han_count += unit_han_count

        if detectable_length < 4:
            continue

        try:
            guesses = detect_langs(detectable_text)
        except Exception:
            continue

        if not guesses:
            continue

        normalized_guesses = []
        total_probability = 0.0
        for guess in guesses:
            normalized_code = _normalize_detected_code(guess.lang)
            if normalized_code is None or guess.prob <= 0:
                continue
            normalized_guesses.append((normalized_code, guess.prob))
            total_probability += guess.prob

        if not normalized_guesses or total_probability <= 0:
            continue

        for normalized_code, probability in normalized_guesses:
            counts[normalized_code] += detectable_length * (probability / total_probability)

    if han_count:
        assigned_code = _assign_han_characters(counts, han_count)
        direct_evidence[assigned_code] += han_count

    if not counts:
        return None

    return _summarize_counts(counts, direct_evidence=direct_evidence)


def _iter_detection_units(text: str) -> list[str]:
    units = []
    for raw in _UNIT_PATTERN.split(text):
        unit = raw.strip()
        if len(unit) >= 8:
            units.append(unit)
    if units:
        return units
    return [text]


def _normalize_detected_code(code: str) -> str | None:
    lowered = code.lower()
    aliases = {
        "zh-cn": "zh-Hans",
        "zh-tw": "zh-Hant",
        "zh": "zh-Hans",
        "iw": "he",
    }
    normalized = aliases.get(lowered, lowered)
    if normalized in LANGUAGE_MAP:
        return normalized
    if normalized.split("-")[0] in LANGUAGE_MAP:
        return normalized.split("-")[0]
    return None


def _summarize_counts(
    counts: Counter[str],
    *,
    direct_evidence: Counter[str] | None = None,
) -> DetectionSummary:
    total = sum(counts.values())
    direct_evidence = direct_evidence or Counter()
    languages = [
        DetectedLanguageStat(
            code=code,
            char_count=max(1, int(round(count))),
            share=round(count / total, 4),
        )
        for code, count in counts.items()
        if count > 0
    ]
    languages.sort(key=lambda item: (-item.char_count, item.code))

    significant = [
        item
        for item in languages
        if item.char_count >= 4 and item.share >= 0.18
    ]

    if len(significant) >= 2:
        return DetectionSummary(mode="mixed", primary_language=None, languages=languages)

    if len(languages) >= 2:
        for item in languages[1:]:
            if direct_evidence.get(item.code, 0) >= 2:
                return DetectionSummary(mode="mixed", primary_language=None, languages=languages)

    if languages and languages[0].share >= 0.55:
        return DetectionSummary(
            mode="single",
            primary_language=languages[0].code,
            languages=languages,
        )

    if len(languages) == 1:
        return DetectionSummary(
            mode="single",
            primary_language=languages[0].code,
            languages=languages,
        )

    return DetectionSummary(mode="unknown", primary_language=None, languages=languages)


def _detect_with_script_fallback(text: str) -> DetectionSummary:
    counts = Counter[str]()
    han_count = 0

    for char in text:
        if _is_ignored(char):
            continue
        if _is_hangul(char):
            counts["ko"] += 1
            continue
        if _is_kana(char):
            counts["ja"] += 1
            continue
        if _is_cjk_ideograph(char):
            han_count += 1
            continue
        if _is_latin(char):
            counts["en"] += 1

    direct_evidence = Counter(counts)
    if han_count:
        assigned_code = _assign_han_characters(counts, han_count)
        direct_evidence[assigned_code] += han_count

    if not counts:
        return DetectionSummary(mode="unknown", primary_language=None, languages=[])
    return _summarize_counts(counts, direct_evidence=direct_evidence)


def _split_detection_unit(unit: str) -> tuple[Counter[str], float, str, int]:
    counts = Counter[str]()
    han_count = 0.0
    detectable_parts: list[str] = []
    detectable_length = 0
    previous_was_space = False

    for char in unit:
        if _is_hangul(char):
            counts["ko"] += 1
            previous_was_space = False
            continue
        if _is_kana(char):
            counts["ja"] += 1
            previous_was_space = False
            continue
        if _is_cjk_ideograph(char):
            han_count += 1
            previous_was_space = False
            continue
        if _is_ignored(char):
            if detectable_parts and not previous_was_space:
                detectable_parts.append(" ")
                previous_was_space = True
            continue

        detectable_parts.append(char)
        detectable_length += 1
        previous_was_space = False

    return counts, han_count, "".join(detectable_parts).strip(), detectable_length


def _assign_han_characters(counts: Counter[str], han_count: float) -> str:
    if counts["ja"] >= max(2, han_count / 8):
        counts["ja"] += han_count
        return "ja"
    if counts["ko"] and han_count <= max(2, counts["ko"] / 8):
        counts["ko"] += han_count
        return "ko"
    counts["zh-Hans"] += han_count
    return "zh-Hans"


def _is_ignored(char: str) -> bool:
    return char.isspace() or char.isdigit() or _is_basic_punctuation(char)


def _is_basic_punctuation(char: str) -> bool:
    return char in {
        ".",
        ",",
        "!",
        "?",
        ":",
        ";",
        "'",
        '"',
        "(",
        ")",
        "[",
        "]",
        "{",
        "}",
        "-",
        "_",
        "/",
        "\\",
        "@",
        "#",
        "$",
        "%",
        "^",
        "&",
        "*",
        "+",
        "=",
        "~",
        "`",
        "|",
        "<",
        ">",
        "…",
        "·",
        "•",
        "、",
        "。",
        "，",
        "！",
        "？",
        "：",
        "；",
    }


def _is_hangul(char: str) -> bool:
    code = ord(char)
    return (
        0x1100 <= code <= 0x11FF
        or 0x3130 <= code <= 0x318F
        or 0xAC00 <= code <= 0xD7AF
    )


def _is_kana(char: str) -> bool:
    code = ord(char)
    return (
        0x3040 <= code <= 0x309F
        or 0x30A0 <= code <= 0x30FF
        or 0x31F0 <= code <= 0x31FF
        or 0xFF66 <= code <= 0xFF9D
    )


def _is_cjk_ideograph(char: str) -> bool:
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
    )


def _is_latin(char: str) -> bool:
    code = ord(char)
    return (
        0x0041 <= code <= 0x005A
        or 0x0061 <= code <= 0x007A
        or 0x00C0 <= code <= 0x024F
    )
