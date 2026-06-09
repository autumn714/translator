from __future__ import annotations

from dataclasses import dataclass

from translator_app.schemas import GlossaryEntry
from translator_app.services.segmentation import split_text


@dataclass(slots=True)
class EngineResult:
    translation: str
    segments: list[str]


class TranslationEngine:
    name: str = "base"

    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        glossary_entries: list[GlossaryEntry] | None = None,
    ) -> EngineResult:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None

    def segment_source_text(self, text: str) -> list[str]:
        return split_text(text)
