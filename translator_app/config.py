from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=7860, alias="APP_PORT")
    cors_allow_origins_raw: str = Field(default="", alias="CORS_ALLOW_ORIGINS")
    translator_home: Path = Field(default=Path("/app"), alias="TRANSLATOR_HOME")
    glossary_path: Path = Field(
        default=Path("/app/data/glossary/default.json"),
        alias="GLOSSARY_PATH",
    )

    engine_type: str = Field(default="mock", alias="ENGINE_TYPE")

    openai_base_url: str = Field(default="http://127.0.0.1:8001/v1", alias="OPENAI_BASE_URL")
    openai_api_key: str = Field(default="EMPTY", alias="OPENAI_API_KEY")
    openai_model: str = Field(
        default="gemma-4-E4B-it",
        alias="OPENAI_MODEL",
    )
    openai_api_mode: str = Field(default="chat", alias="OPENAI_API_MODE")
    openai_system_prompt: str = Field(
        default=(
            "You are a dedicated enterprise translation engine. "
            "Translate multilingual input into the requested target language. "
            "Preserve formatting, lists, and line breaks. "
            "Return only the translated text in the requested target language."
        ),
        alias="OPENAI_SYSTEM_PROMPT",
    )
    openai_temperature: float = Field(default=0.0, alias="OPENAI_TEMPERATURE")
    openai_max_tokens: int = Field(default=768, alias="OPENAI_MAX_TOKENS")
    openai_parallelism: int = Field(default=6, alias="OPENAI_PARALLELISM")
    openai_chunk_chars: int = Field(default=700, alias="OPENAI_CHUNK_CHARS")
    openai_segment_cache_size: int = Field(default=2048, alias="OPENAI_SEGMENT_CACHE_SIZE")

    ct2_device: str = Field(default="cpu", alias="CT2_DEVICE")
    ct2_compute_type: str = Field(default="int8", alias="CT2_COMPUTE_TYPE")
    ct2_inter_threads: int = Field(default=1, alias="CT2_INTER_THREADS")
    ct2_intra_threads: int = Field(default=0, alias="CT2_INTRA_THREADS")
    ct2_model_dir: Path = Field(
        default=Path("/app/models/preview-en-ko-ct2"),
        alias="CT2_MODEL_DIR",
    )
    ct2_sentencepiece_model: Path = Field(
        default=Path("/app/models/preview-en-ko-ct2/source.spm"),
        alias="CT2_SENTENCEPIECE_MODEL",
    )

    @property
    def cors_allow_origins(self) -> list[str]:
        raw = self.cors_allow_origins_raw.strip()
        if not raw:
            return []
        return [item.strip() for item in raw.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
