from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints


LanguageCode = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=16)]
SourceLanguageCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=2, max_length=16),
]

# ko: formal=합니다체, informal=해요체, plain=한다체(평어), gaejoshik=개조식(~함/~임, 명사형 종결)
# ja: formal=です・ます, informal/plain=だ・である ; others: formal/informal ; auto = keep source register
Formality = Literal["auto", "formal", "informal", "plain", "gaejoshik"]
RewriteStyle = Literal["polish", "formal", "concise", "plain", "friendly", "gaejoshik", "academic", "business"]
Priority = Literal["interactive", "document"]


# ---------------------------------------------------------------- glossary
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


# ---------------------------------------------------------------- translation
class TranslateOptions(BaseModel):
    source_lang: SourceLanguageCode = "auto"
    target_lang: LanguageCode = "ko"
    formality: Formality = "auto"
    context: str = Field(default="", max_length=4000)  # DeepL-style extra context (not translated)
    instructions: str = Field(default="", max_length=2000)  # user style rules, one per line
    use_glossary: bool = True
    glossary_id: str | None = None
    glossary_entries: list[GlossaryEntry] | None = None  # explicit override (UI sends unsaved edits)


class TranslationRequest(TranslateOptions):
    # The configurable limit (TEXT_MAX_CHARS) is checked in the router; this is only a hard ceiling.
    text: str = Field(default="", max_length=1_000_000)


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
    detected_source_lang: str | None = None
    formality: Formality = "auto"
    engine: str
    latency_ms: int


class AlternativesRequest(TranslateOptions):
    source: str = Field(default="", max_length=20000)  # paragraph source
    translation: str = Field(min_length=1, max_length=20000)  # current paragraph translation
    span: str = Field(min_length=1, max_length=4000)  # substring of translation to replace


class AlternativesResponse(BaseModel):
    alternatives: list[str]


class RewriteRequest(BaseModel):
    text: str = Field(default="", max_length=10000)
    lang: str = Field(default="auto", max_length=16)
    style: RewriteStyle = "polish"
    context: str = Field(default="", max_length=4000)


class RewriteResponse(BaseModel):
    text: str
    detected_lang: str | None = None


class LookupRequest(BaseModel):
    term: str = Field(min_length=1, max_length=100)
    context: str = Field(default="", max_length=2000)
    source_lang: str = Field(default="auto", max_length=16)
    target_lang: str = Field(default="ko", min_length=2, max_length=16)


class LookupEntry(BaseModel):
    translation: str
    pos: str = ""
    note: str = ""


class LookupExample(BaseModel):
    source: str
    target: str


class LookupResponse(BaseModel):
    term: str
    entries: list[LookupEntry] = Field(default_factory=list)
    examples: list[LookupExample] = Field(default_factory=list)


# ---------------------------------------------------------------- system
class ModelStatus(BaseModel):
    connected: bool = False
    name: str | None = None
    max_model_len: int | None = None
    vision: bool = False
    running: int | None = None
    waiting: int | None = None
    error: str | None = None


class AuthStatus(BaseModel):
    enabled: bool = False
    user: str | None = None


class LimitsStatus(BaseModel):
    text_max_chars: int
    doc_max_mb: int
    doc_retention_hours: int


class StatusResponse(BaseModel):
    app_version: str
    engine: str
    model: ModelStatus
    auth: AuthStatus
    limits: LimitsStatus
    document_formats: list[dict[str, Any]] = Field(default_factory=list)


class LoginRequest(BaseModel):
    username: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=1000)
