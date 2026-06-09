from __future__ import annotations

from pathlib import Path

import ctranslate2
import sentencepiece as spm

from translator_app.config import Settings
from translator_app.engines.base import EngineResult, TranslationEngine
from translator_app.schemas import GlossaryEntry
from translator_app.services.segmentation import normalize_line_breaks, split_translation_units


class CTranslate2Engine(TranslationEngine):
    name = "ctranslate2"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._translator = self._build_translator(settings)
        self._sp = self._load_tokenizer(settings.ct2_sentencepiece_model)

    def _build_translator(self, settings: Settings) -> ctranslate2.Translator:
        return ctranslate2.Translator(
            str(settings.ct2_model_dir),
            device=settings.ct2_device,
            compute_type=settings.ct2_compute_type,
            inter_threads=settings.ct2_inter_threads,
            intra_threads=settings.ct2_intra_threads,
        )

    def _load_tokenizer(self, model_path: Path) -> spm.SentencePieceProcessor:
        processor = spm.SentencePieceProcessor()
        processor.load(str(model_path))
        return processor

    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        glossary_entries: list[GlossaryEntry] | None = None,
    ) -> EngineResult:
        if source_lang not in {"auto", "en"} or target_lang != "ko":
            raise RuntimeError("CTranslate2 preview engine supports only English to Korean translation.")
        if glossary_entries:
            raise RuntimeError("CTranslate2 preview engine does not support glossary entries.")

        units = split_translation_units(text, max_chars=1200)
        if not units:
            return EngineResult(translation="", segments=[])

        source_tokens = [self._sp.encode(segment, out_type=str) for segment in units]
        results = self._translator.translate_batch(source_tokens, beam_size=4)
        translated_units = [
            self._sp.decode(result.hypotheses[0]).strip()
            for result in results
        ]
        translation = normalize_line_breaks("\n\n".join(translated_units))

        return EngineResult(
            translation=translation,
            segments=translated_units,
        )

    def segment_source_text(self, text: str) -> list[str]:
        return split_translation_units(text, max_chars=1200)
