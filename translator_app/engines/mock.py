from __future__ import annotations

from translator_app.engines.base import EngineResult, TranslationEngine
from translator_app.schemas import GlossaryEntry
from translator_app.services.segmentation import normalize_line_breaks, split_translation_units


class MockEngine(TranslationEngine):
    name = "mock"

    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        glossary_entries: list[GlossaryEntry] | None = None,
    ) -> EngineResult:
        units = split_translation_units(text, max_chars=1600)
        translated_units = [f"[mock ko] {unit}" for unit in units]
        return EngineResult(
            translation=normalize_line_breaks("\n\n".join(translated_units)),
            segments=translated_units,
        )

    def segment_source_text(self, text: str) -> list[str]:
        return split_translation_units(text, max_chars=1600)
