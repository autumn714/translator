"""Translation engine interface.

An engine performs single LLM tasks (one unit, one JSON batch, one image ...). Orchestration
(language detection, glossary selection, caching, batching, retries, streaming events) lives
in ``translator_app.services.translator.TranslatorService``.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from translator_app.languages import unit_joiner
from translator_app.llm.prompts import TranslationSpec
from translator_app.schemas import GlossaryEntry
from translator_app.services.glossary import match_glossary_entries
from translator_app.services.segmentation import join_units, split_text, split_units


@dataclass(slots=True)
class EngineResult:
    translation: str
    segments: list[str]


class TranslationEngine:
    name: str = "base"

    # ------------------------------------------------------------------ unit-level API
    async def translate_unit(
        self,
        text: str,
        spec: TranslationSpec,
        *,
        priority: str = "interactive",
        tags: bool = False,
        strict_tags: str | None = None,
        retry: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> str:
        """Translate one unit. ``strict_tags`` → temperature 0 + tag reminder; ``retry`` → anti-repetition.
        ``meta["finish_reason"]`` is set when ``meta`` is given."""
        raise NotImplementedError

    async def stream_unit(
        self,
        text: str,
        spec: TranslationSpec,
        *,
        priority: str = "interactive",
        meta: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """Yield translated text pieces. ``meta["finish_reason"]`` is set at the end."""
        result = await self.translate_unit(text, spec, priority=priority)
        if meta is not None:
            meta["finish_reason"] = "stop"
        yield result

    async def translate_items(
        self,
        items: Sequence[str],
        spec: TranslationSpec,
        *,
        priority: str = "document",
        tags: bool = False,
        preceding: Sequence[tuple[str, str]] | None = None,
    ) -> list[str]:
        """One structured-output request for several items; raises LLMOutputError on a bad answer."""
        raise NotImplementedError

    async def alternatives(self, source: str, translation: str, span: str, spec: TranslationSpec) -> list[str]:
        raise NotImplementedError

    async def rewrite(self, text: str, lang: str | None, style: str, context: str = "") -> str:
        raise NotImplementedError

    async def lookup(self, term: str, context: str, source_lang: str, target_lang: str) -> dict[str, Any]:
        raise NotImplementedError

    async def describe_image(
        self,
        data: bytes,
        mime: str,
        spec: TranslationSpec,
        *,
        mode: str = "translate",
        priority: str = "document",
    ) -> str:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None

    # ------------------------------------------------------------------ legacy whole-text API
    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        glossary_entries: list[GlossaryEntry] | None = None,
    ) -> EngineResult:
        units = split_units(text, max_chars=1200)
        if not units:
            return EngineResult(translation="", segments=[])

        async def one(unit_text: str) -> str:
            hits = match_glossary_entries(unit_text, glossary_entries or [], target_lang=target_lang,
                                          source_lang=source_lang)
            spec = TranslationSpec(source_lang=source_lang, target_lang=target_lang, glossary=tuple(hits))
            return await self.translate_unit(unit_text, spec)

        results = list(await asyncio.gather(*(one(unit.text) for unit in units)))
        return EngineResult(
            translation=join_units(units, results, joiner=unit_joiner(target_lang)),
            segments=results,
        )

    def segment_source_text(self, text: str) -> list[str]:
        return split_text(text)
