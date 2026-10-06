from __future__ import annotations

import json
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _env(*names: str) -> AliasChoices:
    return AliasChoices(*names)


class Settings(BaseSettings):
    """Runtime settings read from environment variables (see SPEC §4).

    Attribute names are the env names lower-cased, except TRANSLATOR_DATA_DIR -> data_dir.
    Old OPENAI_* names are accepted as fallbacks.
    """

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    app_host: str = Field(default="0.0.0.0", validation_alias=_env("APP_HOST", "app_host"))
    app_port: int = Field(default=7860, validation_alias=_env("APP_PORT", "app_port"))

    data_dir: Path = Field(default=Path("./data"), validation_alias=_env("TRANSLATOR_DATA_DIR", "data_dir"))
    glossary_path: Path | None = Field(default=None, validation_alias=_env("GLOSSARY_PATH", "glossary_path"))

    engine_type: str = Field(default="openai_compatible", validation_alias=_env("ENGINE_TYPE", "engine_type"))

    llm_base_url: str = Field(
        default="http://llm:8000/v1",
        validation_alias=_env("LLM_BASE_URL", "OPENAI_BASE_URL", "llm_base_url"),
    )
    llm_api_key: str = Field(default="EMPTY", validation_alias=_env("LLM_API_KEY", "OPENAI_API_KEY", "llm_api_key"))
    llm_model: str = Field(default="", validation_alias=_env("LLM_MODEL", "OPENAI_MODEL", "llm_model"))
    llm_timeout: float = Field(default=600.0, gt=0, validation_alias=_env("LLM_TIMEOUT", "llm_timeout"))
    llm_max_parallel: int = Field(
        default=3,
        ge=1,
        validation_alias=_env("LLM_MAX_PARALLEL", "OPENAI_PARALLELISM", "llm_max_parallel"),
    )
    llm_doc_parallel: int = Field(default=2, ge=1, validation_alias=_env("LLM_DOC_PARALLEL", "llm_doc_parallel"))
    llm_temperature: float = Field(default=0.2, ge=0, le=2, validation_alias=_env("LLM_TEMPERATURE", "llm_temperature"))
    llm_extra_body: Annotated[dict[str, Any], NoDecode] = Field(
        default_factory=dict,
        validation_alias=_env("LLM_EXTRA_BODY", "llm_extra_body"),
    )
    llm_vision: Literal["auto", "on", "off"] = Field(default="auto", validation_alias=_env("LLM_VISION", "llm_vision"))

    text_max_chars: int = Field(default=30000, ge=1, validation_alias=_env("TEXT_MAX_CHARS", "text_max_chars"))
    text_chunk_chars: int = Field(default=1200, ge=100, validation_alias=_env("TEXT_CHUNK_CHARS", "text_chunk_chars"))
    segment_cache_size: int = Field(
        default=4096,
        ge=0,
        validation_alias=_env("SEGMENT_CACHE_SIZE", "OPENAI_SEGMENT_CACHE_SIZE", "segment_cache_size"),
    )

    doc_max_mb: int = Field(default=50, ge=1, validation_alias=_env("DOC_MAX_MB", "doc_max_mb"))
    doc_max_chars: int = Field(default=600000, ge=1, validation_alias=_env("DOC_MAX_CHARS", "doc_max_chars"))
    doc_retention_hours: int = Field(default=24, ge=1, validation_alias=_env("DOC_RETENTION_HOURS", "doc_retention_hours"))
    doc_job_concurrency: int = Field(default=2, ge=1, validation_alias=_env("DOC_JOB_CONCURRENCY", "doc_job_concurrency"))

    ui_auth: bool = Field(default=False, validation_alias=_env("UI_AUTH", "ui_auth"))
    ui_user: str = Field(default="translator", validation_alias=_env("UI_USER", "ui_user"))
    ui_password: str = Field(default="", validation_alias=_env("UI_PASSWORD", "ui_password"))
    session_secret: str = Field(default="", validation_alias=_env("SESSION_SECRET", "session_secret"))

    cors_allow_origins: str = Field(default="", validation_alias=_env("CORS_ALLOW_ORIGINS", "cors_allow_origins"))

    @field_validator(
        "app_port",
        "data_dir",
        "glossary_path",
        "llm_timeout",
        "llm_max_parallel",
        "llm_doc_parallel",
        "llm_temperature",
        "text_max_chars",
        "text_chunk_chars",
        "segment_cache_size",
        "doc_max_mb",
        "doc_max_chars",
        "doc_retention_hours",
        "doc_job_concurrency",
        "ui_auth",
        mode="before",
    )
    @classmethod
    def _empty_means_default(cls, value: Any, info: ValidationInfo) -> Any:
        # run.sh passes every variable, so an unset config value arrives as "" — use the default.
        if isinstance(value, str) and not value.strip():
            assert info.field_name is not None
            return cls.model_fields[info.field_name].get_default(call_default_factory=True)
        return value

    @field_validator("llm_extra_body", mode="before")
    @classmethod
    def _parse_extra_body(cls, value: Any) -> Any:
        if value is None:
            return {}
        if isinstance(value, (bytes, str)):
            raw = value.decode() if isinstance(value, bytes) else value
            if not raw.strip():
                return {}
            try:
                value = json.loads(raw)
            except ValueError as exc:
                raise ValueError("LLM_EXTRA_BODY 는 JSON 객체여야 합니다") from exc
        if not isinstance(value, dict):
            raise ValueError("LLM_EXTRA_BODY 는 JSON 객체여야 합니다")
        return value

    @field_validator("llm_vision", mode="before")
    @classmethod
    def _normalize_vision(cls, value: Any) -> Any:
        if isinstance(value, str):
            lowered = value.strip().lower()
            aliases = {"1": "on", "true": "on", "yes": "on", "0": "off", "false": "off", "no": "off", "": "auto"}
            return aliases.get(lowered, lowered)
        return value

    @field_validator("engine_type", mode="before")
    @classmethod
    def _normalize_engine(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _fill_derived(self) -> Settings:
        if self.glossary_path is None:
            self.glossary_path = self.data_dir / "glossary" / "default.json"
        if not self.session_secret:
            self.session_secret = secrets.token_hex(32)
        if self.llm_doc_parallel > self.llm_max_parallel:
            self.llm_doc_parallel = self.llm_max_parallel
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        raw = self.cors_allow_origins.strip()
        if not raw:
            return []
        return [item.strip() for item in raw.split(",") if item.strip()]

    @property
    def glossary_file(self) -> Path:
        assert self.glossary_path is not None
        return self.glossary_path

    @property
    def session_key(self) -> bytes:
        """HMAC key: SESSION_SECRET as hex (run.sh persists one in state/), else its UTF-8 bytes."""
        raw = self.session_secret.strip()
        try:
            key = bytes.fromhex(raw)
        except ValueError:
            key = b""
        return key if len(key) >= 16 else raw.encode("utf-8")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
