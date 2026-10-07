from __future__ import annotations

from translator_app.config import Settings
from translator_app.engines.base import TranslationEngine
from translator_app.engines.mock import MockEngine
from translator_app.llm.client import LLMClient


def build_engine(settings: Settings, llm: LLMClient | None = None) -> TranslationEngine:
    engine_type = settings.engine_type.lower()
    if engine_type == "mock":
        return MockEngine()
    if engine_type == "openai_compatible":
        from translator_app.engines.openai_compatible import OpenAICompatibleEngine

        return OpenAICompatibleEngine(settings, llm or LLMClient(settings))
    raise ValueError(f"지원하지 않는 ENGINE_TYPE 입니다: {settings.engine_type} (openai_compatible 또는 mock)")
