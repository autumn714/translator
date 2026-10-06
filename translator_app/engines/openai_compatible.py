"""Engine backed by an OpenAI-compatible server (vLLM + Qwen3.8) through LLMClient."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from translator_app.config import Settings
from translator_app.engines.base import TranslationEngine
from translator_app.llm import prompts
from translator_app.llm.client import (
    ALTERNATIVES_SAMPLING,
    JSON_SAMPLING,
    MAX_COMPLETION_TOKENS,
    OCR_MAX_COMPLETION_TOKENS,
    OCR_SAMPLING,
    RETRY_FINISH_REASONS,
    REWRITE_SAMPLING,
    TRANSLATE_SAMPLING,
    LLMClient,
    LLMOutputError,
    VisionUnavailable,
    completion_budget,
    estimate_tokens,
    image_data_uri,
)
from translator_app.llm.prompts import TranslationSpec

logger = logging.getLogger("translator.engine")

STRICT_SAMPLING = JSON_SAMPLING  # temperature 0, top_p 1.0, top_k -1


class OpenAICompatibleEngine(TranslationEngine):
    name = "openai_compatible"

    def __init__(self, settings: Settings, llm: LLMClient) -> None:
        self._settings = settings
        self.llm = llm
        # LLM_TEMPERATURE tunes plain/streamed translation; everything else follows SPEC §6a presets
        self._translate_sampling = TRANSLATE_SAMPLING.with_(temperature=float(settings.llm_temperature))

    async def translate_unit(
        self,
        text: str,
        spec: TranslationSpec,
        *,
        priority: str = "interactive",
        tags: bool = False,
        strict_tags: str | None = None,
        retry: bool = False,
    ) -> str:
        messages = prompts.translation_messages(text, spec, tags=tags, strict_tags=strict_tags)
        sampling = STRICT_SAMPLING if strict_tags else self._translate_sampling
        budget = completion_budget(text)
        if retry:
            sampling = sampling.with_(presence_penalty=1.0)
            budget = min(MAX_COMPLETION_TOKENS, budget * 2)
        result = await self.llm.chat(
            messages,
            sampling=sampling,
            max_tokens=budget,
            priority=priority,
            retry_truncated=not retry,
        )
        if result.finish_reason in RETRY_FINISH_REASONS:
            logger.warning("번역 출력이 끊겼습니다 (%s, 원문 %d자)", result.finish_reason, len(text))
        return result.content

    async def stream_unit(
        self,
        text: str,
        spec: TranslationSpec,
        *,
        priority: str = "interactive",
        meta: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        stream = self.llm.chat_stream(
            prompts.translation_messages(text, spec),
            sampling=self._translate_sampling,
            max_tokens=completion_budget(text),
            priority=priority,
            meta=meta,
        )
        try:
            async for piece in stream:
                yield piece
        finally:
            await stream.aclose()  # closes the upstream response → vLLM aborts the request

    async def translate_items(
        self,
        items: Sequence[str],
        spec: TranslationSpec,
        *,
        priority: str = "document",
        tags: bool = False,
        preceding: Sequence[tuple[str, str]] | None = None,
    ) -> list[str]:
        count = len(items)
        if count == 0:
            return []
        messages = prompts.batch_messages(items, spec, tags=tags, preceding=preceding)
        source_tokens = sum(estimate_tokens(item) for item in items) + 12 * count
        value = await self.llm.chat_json(
            messages,
            schema=prompts.batch_schema(count),
            name="translations",
            max_tokens=completion_budget(source_tokens),
            sampling=JSON_SAMPLING,
            priority=priority,
        )
        rows = value["translations"]
        ids = [row["id"] for row in rows]
        if ids != list(range(count)):
            if sorted(ids) != list(range(count)):
                raise LLMOutputError("모델 응답의 항목 번호가 맞지 않습니다.")
            rows = sorted(rows, key=lambda row: row["id"])
        return [row["output"] for row in rows]

    async def alternatives(self, source: str, translation: str, span: str, spec: TranslationSpec) -> list[str]:
        count = prompts.ALTERNATIVES_COUNT
        value = await self.llm.chat_json(
            prompts.alternatives_messages(source, translation, span, spec, count),
            schema=prompts.alternatives_schema(count),
            name="alternatives",
            max_tokens=completion_budget(estimate_tokens(span) * count + 16 * count),
            sampling=ALTERNATIVES_SAMPLING,
            priority="interactive",
        )
        return [str(item) for item in value["alternatives"]]

    async def rewrite(self, text: str, lang: str | None, style: str, context: str = "") -> str:
        result = await self.llm.chat(
            prompts.rewrite_messages(text, lang, style, context),
            sampling=REWRITE_SAMPLING,
            max_tokens=completion_budget(text),
            priority="interactive",
        )
        return result.content

    async def lookup(self, term: str, context: str, source_lang: str, target_lang: str) -> dict[str, Any]:
        value = await self.llm.chat_json(
            prompts.lookup_messages(term, context, source_lang, target_lang),
            schema=prompts.LOOKUP_SCHEMA,
            name="dictionary",
            max_tokens=1024,
            sampling=JSON_SAMPLING,
            priority="interactive",
        )
        return value if isinstance(value, dict) else {}

    async def describe_image(
        self,
        data: bytes,
        mime: str,
        spec: TranslationSpec,
        *,
        mode: str = "translate",
        priority: str = "document",
    ) -> str:
        if not self.llm.vision_enabled:
            raise VisionUnavailable()
        data_uri = await asyncio.to_thread(image_data_uri, data, mime)
        result = await self.llm.chat(
            prompts.image_messages(data_uri, spec, mode=mode),
            sampling=OCR_SAMPLING,
            max_tokens=OCR_MAX_COMPLETION_TOKENS,
            priority=priority,
            max_tokens_cap=OCR_MAX_COMPLETION_TOKENS,
        )
        return result.content
