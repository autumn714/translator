from __future__ import annotations

from typing import TypedDict


class LanguageSpec(TypedDict, total=False):
    code: str
    label: str
    prompt_name: str
    favorite: bool
    formality: list[str]


# Register ("어조") choices per target language; the prompt wording lives in llm/prompts.py.
# ko: formal=합니다체, informal=해요체, plain=한다체, gaejoshik=개조식 / ja: formal=です・ます, plain=だ・である
FORMALITY_OPTIONS: dict[str, list[str]] = {
    "ko": ["auto", "formal", "informal", "plain", "gaejoshik"],
    "ja": ["auto", "formal", "plain"],
}
DEFAULT_FORMALITY_OPTIONS = ["auto", "formal", "informal"]


LANGUAGES: list[LanguageSpec] = [
    {"code": "ko", "label": "한국어", "prompt_name": "Korean", "favorite": True},
    {"code": "en", "label": "영어", "prompt_name": "English", "favorite": True},
    {"code": "ja", "label": "일본어", "prompt_name": "Japanese", "favorite": True},
    {"code": "zh-Hans", "label": "중국어(간체)", "prompt_name": "Chinese (Simplified)", "favorite": True},
    {"code": "zh-Hant", "label": "중국어(번체)", "prompt_name": "Chinese (Traditional)", "favorite": True},
    {"code": "ar", "label": "아랍어", "prompt_name": "Arabic", "favorite": False},
    {"code": "bg", "label": "불가리아어", "prompt_name": "Bulgarian", "favorite": False},
    {"code": "bn", "label": "벵골어", "prompt_name": "Bengali", "favorite": False},
    {"code": "cs", "label": "체코어", "prompt_name": "Czech", "favorite": False},
    {"code": "da", "label": "덴마크어", "prompt_name": "Danish", "favorite": False},
    {"code": "de", "label": "독일어", "prompt_name": "German", "favorite": False},
    {"code": "el", "label": "그리스어", "prompt_name": "Greek", "favorite": False},
    {"code": "es", "label": "스페인어", "prompt_name": "Spanish", "favorite": False},
    {"code": "et", "label": "에스토니아어", "prompt_name": "Estonian", "favorite": False},
    {"code": "fa", "label": "페르시아어", "prompt_name": "Persian", "favorite": False},
    {"code": "fi", "label": "핀란드어", "prompt_name": "Finnish", "favorite": False},
    {"code": "fil", "label": "필리핀어", "prompt_name": "Filipino", "favorite": False},
    {"code": "fr", "label": "프랑스어", "prompt_name": "French", "favorite": False},
    {"code": "he", "label": "히브리어", "prompt_name": "Hebrew", "favorite": False},
    {"code": "hi", "label": "힌디어", "prompt_name": "Hindi", "favorite": False},
    {"code": "hr", "label": "크로아티아어", "prompt_name": "Croatian", "favorite": False},
    {"code": "hu", "label": "헝가리어", "prompt_name": "Hungarian", "favorite": False},
    {"code": "id", "label": "인도네시아어", "prompt_name": "Indonesian", "favorite": False},
    {"code": "it", "label": "이탈리아어", "prompt_name": "Italian", "favorite": False},
    {"code": "lt", "label": "리투아니아어", "prompt_name": "Lithuanian", "favorite": False},
    {"code": "lv", "label": "라트비아어", "prompt_name": "Latvian", "favorite": False},
    {"code": "ms", "label": "말레이어", "prompt_name": "Malay", "favorite": False},
    {"code": "nl", "label": "네덜란드어", "prompt_name": "Dutch", "favorite": False},
    {"code": "no", "label": "노르웨이어", "prompt_name": "Norwegian", "favorite": False},
    {"code": "pl", "label": "폴란드어", "prompt_name": "Polish", "favorite": False},
    {"code": "pt", "label": "포르투갈어", "prompt_name": "Portuguese", "favorite": False},
    {"code": "ro", "label": "루마니아어", "prompt_name": "Romanian", "favorite": False},
    {"code": "ru", "label": "러시아어", "prompt_name": "Russian", "favorite": False},
    {"code": "sk", "label": "슬로바키아어", "prompt_name": "Slovak", "favorite": False},
    {"code": "sl", "label": "슬로베니아어", "prompt_name": "Slovenian", "favorite": False},
    {"code": "sr", "label": "세르비아어", "prompt_name": "Serbian", "favorite": False},
    {"code": "sv", "label": "스웨덴어", "prompt_name": "Swedish", "favorite": False},
    {"code": "sw", "label": "스와힐리어", "prompt_name": "Swahili", "favorite": False},
    {"code": "th", "label": "태국어", "prompt_name": "Thai", "favorite": False},
    {"code": "tr", "label": "터키어", "prompt_name": "Turkish", "favorite": False},
    {"code": "uk", "label": "우크라이나어", "prompt_name": "Ukrainian", "favorite": False},
    {"code": "vi", "label": "베트남어", "prompt_name": "Vietnamese", "favorite": False},
]

for _spec in LANGUAGES:
    _spec["formality"] = list(FORMALITY_OPTIONS.get(_spec["code"], DEFAULT_FORMALITY_OPTIONS))

LANGUAGE_MAP = {item["code"]: item for item in LANGUAGES}
# Languages written without spaces between sentences (units are joined with "").
NO_SPACE_LANGUAGES = frozenset({"ja", "zh-Hans", "zh-Hant", "th"})
FAVORITE_LANGUAGE_CODES = [item["code"] for item in LANGUAGES if item["favorite"]]


def language_name(code: str) -> str:
    return LANGUAGE_MAP.get(code, {}).get("prompt_name", code)


def language_label(code: str) -> str:
    return LANGUAGE_MAP.get(code, {}).get("label", code)


def is_known_language(code: str) -> bool:
    return code in LANGUAGE_MAP


def unit_joiner(target_lang: str) -> str:
    """Separator between sentence chunks of one paragraph in the target language."""
    return "" if target_lang in NO_SPACE_LANGUAGES else " "
