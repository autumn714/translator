from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
import threading
from typing import Literal

from translator_app.languages import LANGUAGE_MAP

try:
    from langdetect import DetectorFactory, detect_langs

    DetectorFactory.seed = 0
except Exception:  # pragma: no cover - fallback path when dependency is unavailable.
    detect_langs = None


LanguageMode = Literal["single", "mixed", "unknown"]

_UNIT_PATTERN = re.compile(r"\n{2,}|(?<=[.!?。！？])\s+")

# Frequent characters written differently in Traditional and Simplified Chinese (each set holds only
# characters that the other script does not use), to tell zh-Hant from zh-Hans for Han-only text.
_TRADITIONAL_ONLY = frozenset(
    "這個們來時會說對為國學與點發現經進過還開問關認實當從動樣種義長電無車書東門見頭萬語業產務網際華機將氣處變應該"
    "總聯廣師歷錢買賣讓雙請議題號風飛馬魚鳥麗黃齊龍區員圖報場壓濟熱爾環結給統續練線組細終級紅約紀術規視計記設許試"
    "話調論識證讀負貨質費資選運達適邊釋錄鐵間陽隊離難響項須領類顯飯驗價優傳億兒創勞勢單廠參嗎園團備媽寫導層屬島帶"
    "幫庫張歸態戰擇數斷條極構標樂權歡測滿漢燈營獨獲畫療確穩競筆節簡糧純紙絕維綠緊縣織職聽腦興舉舊藝蘭衛裝親觀覺訂"
    "訊詞詳誤課談謝講護贊貝財責貴貿賽趙軍軟較載輕輸轉農連遠遺鄉醫針鋼錯鍵閱隨險雜雖雞韓頁順預頻額顧養館氫換"
    "製後裡臺範復複據劃異豐願週遊內體麼"
)
_SIMPLIFIED_ONLY = frozenset(
    "这个们来时会说对为国学与点发现经进过还开问关认实当从动样种义长电无车书东门见头万语业产务网际华机将气处变应该"
    "总联广师历钱买卖让双请议题号风飞马鱼鸟丽黄齐龙区员图报场压济热尔环结给统续练线组细终级红约纪术规视计记设许试"
    "话调论识证读负货质费资选运达适边释录铁间阳队离难响项须领类显饭验价优传亿儿创劳势单厂参吗园团备妈写导层属岛带"
    "帮库张归态战择数断条极构标乐权欢测满汉灯营独获画疗确稳竞笔节简粮纯纸绝维绿紧县织职听脑兴举旧艺兰卫装亲观觉订"
    "讯词详误课谈谢讲护赞贝财责贵贸赛赵军软较载轻输转农连远遗乡医针钢错键阅随险杂虽鸡韩页顺预频额顾养馆氢换"
)

_FACTORY_LOCK = threading.Lock()
_factory_ready = False


def chinese_script_counts(text: str) -> tuple[int, int]:
    """(Traditional-only, Simplified-only) character counts."""
    traditional = simplified = 0
    for char in text:
        if char in _TRADITIONAL_ONLY:
            traditional += 1
        elif char in _SIMPLIFIED_ONLY:
            simplified += 1
    return traditional, simplified


def _chinese_code(text: str) -> str:
    traditional, simplified = chinese_script_counts(text)
    return "zh-Hant" if traditional > simplified else "zh-Hans"


def _ensure_langdetect_ready() -> None:
    """Load langdetect's profiles once under a lock: detection runs in worker threads, and two first
    calls at the same time could otherwise use a half-loaded profile set."""
    global _factory_ready
    if _factory_ready:
        return
    with _FACTORY_LOCK:
        if _factory_ready:
            return
        try:
            from langdetect import detector_factory

            detector_factory.init_factory()
        except Exception:  # noqa: BLE001 - detect_langs reports the problem per call
            pass
        _factory_ready = True


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
    _ensure_langdetect_ready()

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
        assigned_code = _assign_han_characters(counts, han_count, chinese=_chinese_code(text))
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
        assigned_code = _assign_han_characters(counts, han_count, chinese=_chinese_code(text))
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


def _assign_han_characters(counts: Counter[str], han_count: float, *, chinese: str = "zh-Hans") -> str:
    if counts["ja"] >= max(2, han_count / 8):
        counts["ja"] += han_count
        return "ja"
    if counts["ko"] and han_count <= max(2, counts["ko"] / 8):
        counts["ko"] += han_count
        return "ko"
    counts[chinese] += han_count
    return chinese


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
