from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints


LanguageCode = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=16)]
SourceLanguageCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=2, max_length=16),
]


class GlossaryEntry(BaseModel):
    source_lang: LanguageCode = "en"
    target_lang: LanguageCode = "ko"
    source: str = Field(min_length=1, max_length=200)
    target: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=300)
    enabled: bool = True


class GlossaryDocument(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    entries: list[GlossaryEntry] = Field(default_factory=list)


class GlossarySummary(BaseModel):
    id: str
    name: str
    entry_count: int


class GlossaryListResponse(BaseModel):
    glossaries: list[GlossarySummary]
    default_glossary_id: str | None = None


class GlossaryResponse(BaseModel):
    glossary: GlossaryDocument


class GlossaryCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    entries: list[GlossaryEntry] = Field(default_factory=list)


class GlossaryUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    entries: list[GlossaryEntry] = Field(default_factory=list)


class TranslationRequest(BaseModel):
    text: str = Field(default="", max_length=20000)
    source_lang: SourceLanguageCode = "auto"
    target_lang: LanguageCode = "ko"
    use_glossary: bool = True
    glossary_id: str | None = None
    glossary_entries: list[GlossaryEntry] | None = None


class TranslationSegment(BaseModel):
    source: str
    target: str


class DetectedLanguage(BaseModel):
    code: LanguageCode
    char_count: int = Field(ge=0)
    share: float = Field(ge=0.0, le=1.0)


SourceLanguageMode = Literal["single", "mixed", "unknown"]


class TranslationResponse(BaseModel):
    translation: str
    segments: list[TranslationSegment]
    glossary_hits: list[GlossaryEntry]
    glossary_applied: bool
    glossary_name: str | None = None
    detected_source_languages: list[DetectedLanguage] = Field(default_factory=list)
    primary_source_lang: LanguageCode | None = None
    source_language_mode: SourceLanguageMode = "unknown"
    engine: str
    latency_ms: int
