"""Rule-based quality report (검수 결과) computed after a document is translated.

Per translated unit:
  - numbers present in the source but missing in the translation
    (thousands separators / decimal commas normalised; magnitude words skipped)
  - glossary entries whose source term occurs but whose target term is missing
  - output identical to a non-trivial source although the languages differ
  - inline formatting tags lost (the recipe fell back to plain text)
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation

from translator_app.documents.base import has_letters, plain_output, strip_tags, tags_ok

MAX_ITEMS = 500
MAX_TEXT = 500

# 1,234,567.89 | 1.234.567,89 | 1 234 567 | 12.5 | 12,5 | 2024
_NUM = re.compile(r"(?<![\w.,])(\d{1,3}(?:([,.\u00a0\u202f ])\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)(?!\d)")
_MAGNITUDE = re.compile(
    r"^\s*(?:thousand|million|billion|trillion|bn|mn|k|m|b|mio|mrd|milliard|万|萬|億|亿|兆|千|百|만|억|조|천|백)(?![A-Za-z])",
    re.I,
)
_ORDINAL = re.compile(r"^(?:st|nd|rd|th|er|e|ème|º|ª)(?![A-Za-z])", re.I)
_SCALED = re.compile(
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?)\s*(만|억|조|천|万|萬|億|亿|兆|千|thousand|million|billion|trillion)",
    re.I,
)
_GROUPED = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?")
# dates: the month may be spelled out in the translation ("March 15, 2024"), so
# only the year and the day are checked
_DATE_YMD = re.compile(
    r"(?<![\d.,])((?:19|20)\d{2})\s*([./-])\s*(?:1[0-2]|0?[1-9])\s*\2\s*(3[01]|[12]\d|0?[1-9])(?!\d)\.?"
)
_DATE_YM = re.compile(r"(?<![\d.,])((?:19|20)\d{2})\s*[./-]\s*(?:1[0-2]|0[1-9])(?![\d.,]\d)")
_MONTH = re.compile(r"(?<![\d.,])(?:1[0-2]|0?[1-9])\s*(?:월|月)")
_SCALE = {"천": 10**3, "千": 10**3, "thousand": 10**3, "만": 10**4, "万": 10**4, "萬": 10**4,
          "million": 10**6, "억": 10**8, "億": 10**8, "亿": 10**8, "billion": 10**9,
          "조": 10**12, "兆": 10**12, "trillion": 10**12}
_HANGUL = re.compile(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]")
_KANA = re.compile(r"[\u3040-\u30ff]")
_HAN = re.compile(r"[\u4e00-\u9fff]")
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")
_LATIN = re.compile(r"[A-Za-z\u00c0-\u024f]")


def _mask_dates(text: str) -> str:
    text = _DATE_YMD.sub(r" \1 \3 ", text)
    text = _DATE_YM.sub(r" \1 ", text)
    return _MONTH.sub(" ", text)


def _normalize_number(raw: str, sep: str | None) -> str:
    s = raw
    if sep:                                             # grouped number: sep is the thousands separator
        s = s.replace(sep, "")
        if sep == ".":
            s = s.replace(",", ".")                      # 1.234,5 -> 1234.5
    elif re.fullmatch(r"\d+,\d+", s):
        s = s.replace(",", ".")                          # decimal comma (12,5)
    if "." in s:
        whole, frac = s.split(".", 1)
        frac = frac.rstrip("0")
        whole = whole.lstrip("0") or "0"
        return f"{whole}.{frac}" if frac else whole
    return s.lstrip("0") or "0"


def numbers(text: str, *, skip_magnitude: bool = False) -> set[str]:
    """Normalised numbers of ``text``; skip_magnitude ignores "3 million", "1st"..."""
    out: set[str] = set()
    for m in _NUM.finditer(text):
        tail = text[m.end():m.end() + 12]
        if skip_magnitude and (_MAGNITUDE.match(tail) or _ORDINAL.match(tail)):
            continue
        out.add(_normalize_number(m.group(1), m.group(2)))
    return out


def _target_numbers(text: str) -> set[str]:
    found = numbers(text) | {_normalize_number(n, None) for n in re.findall(r"\d+", text)}
    for m in _SCALED.finditer(text):                    # "300만" == 3,000,000 ; "3,500만" == 35,000,000
        raw = m.group(1)
        raw = raw.replace(",", "") if _GROUPED.fullmatch(raw) else raw.replace(",", ".")
        try:
            value = Decimal(raw) * _SCALE[m.group(2).lower()]
        except (KeyError, InvalidOperation):
            continue
        found.add(_normalize_number(format(value, "f"), None))
    return found


def missing_numbers(src: str, tgt: str) -> list[str]:
    src_nums = numbers(_mask_dates(src), skip_magnitude=True)
    if not src_nums:
        return []
    tgt_nums = _target_numbers(tgt)
    return sorted((n for n in src_nums if n not in tgt_nums), key=lambda x: (len(x), x))


def looks_like(text: str, lang: str) -> bool:
    """Rough script check: is ``text`` already written in ``lang``?"""
    lang = (lang or "").lower().split("-")[0]
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    n = len(letters)
    hangul = len(_HANGUL.findall(text))
    kana = len(_KANA.findall(text))
    han = len(_HAN.findall(text))
    cyr = len(_CYRILLIC.findall(text))
    latin = len(_LATIN.findall(text))
    if lang == "ko":
        return hangul / n > 0.3
    if lang == "ja":
        return kana > 0 or (han / n > 0.5 and hangul == 0)
    if lang == "zh":
        return han / n > 0.5 and kana == 0 and hangul == 0
    if lang in ("ru", "uk", "bg", "sr", "kk", "mn"):
        return cyr / n > 0.5
    return latin / n > 0.5                               # Latin-script targets: cannot tell en/fr/de apart


def _norm_term(s: str) -> str:
    return unicodedata.normalize("NFKC", s).casefold()


def _contains(text: str, term: str) -> bool:
    t = _norm_term(term).strip()
    if not t:
        return False
    hay = _norm_term(text)
    if re.fullmatch(r"[\w\- ]+", t, re.ASCII):
        return re.search(rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])", hay) is not None
    return t in hay


_LATIN_WORD = re.compile(r"[A-Za-zÀ-ɏ]{2,}")


def _worth_translating(s: str) -> bool:
    """False for names, codes and titles that legitimately stay unchanged."""
    letters = sum(c.isalpha() for c in s)
    if letters < 3 or len(s) < 4:
        return False
    if len(_LATIN.findall(s)) / letters > 0.5:
        words = _LATIN_WORD.findall(s)
        return len(words) >= 2 and any(w.islower() for w in words)
    return True


def _clip(s: str) -> str:
    s = s.strip()
    return s if len(s) <= MAX_TEXT else s[: MAX_TEXT - 1] + "…"


def build_report(
    pairs: Iterable[tuple[str, str]],
    *,
    source_lang: str,
    target_lang: str,
    glossary: Iterable = (),
    limit: int = MAX_ITEMS,
) -> list[dict]:
    """pairs: (source, translation) with inline tags; glossary: GlossaryEntry-like
    objects (source/target/enabled) already filtered to the translation direction."""
    terms = [(g.source, g.target) for g in glossary if getattr(g, "enabled", True) and g.source and g.target]
    src_code = (source_lang or "auto").lower()
    tgt_code = (target_lang or "").lower()
    same_lang = src_code != "auto" and src_code.split("-")[0] == tgt_code.split("-")[0]
    items: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for src, tgt in pairs:
        if len(items) >= limit:
            break
        if (src, tgt) in seen:
            continue
        seen.add((src, tgt))
        s, t = strip_tags(src).strip(), plain_output(src, tgt).strip()
        if not s or not has_letters(s):
            continue
        issues: list[str] = []
        if not same_lang and s == t and _worth_translating(s):
            if src_code != "auto" or not looks_like(s, tgt_code):
                issues.append("번역되지 않음")
        if s != t:
            miss = missing_numbers(s, t)
            if miss:
                issues.append("숫자 누락: " + ", ".join(miss[:6]) + (" 등" if len(miss) > 6 else ""))
        for term_src, term_tgt in terms:
            if _contains(s, term_src) and not _contains(t, term_tgt):
                issues.append(f"용어집 미적용: {term_src} → {term_tgt}")
                if len(issues) > 4:
                    break
        if not tags_ok(src, tgt):
            issues.append("서식 태그 불일치: 일부 서식이 사라졌을 수 있음")
        for issue in issues:
            if len(items) >= limit:
                break
            items.append({"source": _clip(s), "target": _clip(t), "issue": issue})
    return items
