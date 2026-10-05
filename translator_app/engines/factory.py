from __future__ import annotations

from translator_app.config import Settings
from translator_app.engines.base import TranslationEngine
from translator_app.engines.mock import MockEngine
from translator_app.engines.openai_compatible import OpenAICompatibleEngine


def build_engine(settings: Settings) -> TranslationEngine:
    engine_type = settings.engine_type.lower()
    if engine_type == "mock":
        return MockEngine()
    if engine_type == "openai_compatible":
        return OpenAICompatibleEngine(settings)
    raise ValueError(f"Unsupported ENGINE_TYPE: {settings.engine_type}")
