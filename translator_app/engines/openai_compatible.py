from __future__ import annotations

import asyncio
from collections import OrderedDict

import httpx

from translator_app.config import Settings
from translator_app.engines.base import EngineResult, TranslationEngine
from translator_app.languages import language_name
from translator_app.schemas import GlossaryEntry
from translator_app.services.glossary import match_glossary_entries
from translator_app.services.segmentation import normalize_line_breaks, split_translation_units


class OpenAICompatibleEngine(TranslationEngine):
    name = "openai_compatible"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            timeout=120.0,
            limits=httpx.Limits(max_keepalive_connections=32, max_connections=32),
        )
        self._segment_cache: OrderedDict[str, str] = OrderedDict()
        self._inflight_translations: dict[str, asyncio.Task[str]] = {}
        self._inflight_lock = asyncio.Lock()

    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        glossary_entries: list[GlossaryEntry] | None = None,
    ) -> EngineResult:
        units = split_translation_units(text, max_chars=self._settings.openai_chunk_chars)
        if not units:
            return EngineResult(translation="", segments=[])

        parallelism = max(1, self._settings.openai_parallelism)
        semaphore = asyncio.Semaphore(parallelism)
        tasks = [
            self._translate_segment(
                client=self._client,
                semaphore=semaphore,
                index=index,
                segment=unit,
                source_lang=source_lang,
                target_lang=target_lang,
                glossary_entries=glossary_entries or [],
            )
            for index, unit in enumerate(units)
        ]
        ordered = await asyncio.gather(*tasks)
        ordered.sort(key=lambda item: item[0])
        responses = [content for _, content in ordered]

        translation = normalize_line_breaks("\n\n".join(content.strip() for content in responses))
        return EngineResult(
            translation=translation,
            segments=responses,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _translate_segment(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        index: int,
        segment: str,
        source_lang: str,
        target_lang: str,
        glossary_entries: list[GlossaryEntry],
    ) -> tuple[int, str]:
        segment_glossary = match_glossary_entries(
            segment,
            glossary_entries,
            target_lang=target_lang,
            source_lang=source_lang,
        )
        cache_key = self._build_cache_key(segment, source_lang, target_lang, segment_glossary)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return index, cached

        content = await self._get_or_create_inflight_translation(
            cache_key,
            client=client,
            semaphore=semaphore,
            segment=segment,
            source_lang=source_lang,
            target_lang=target_lang,
            glossary_entries=segment_glossary,
        )
        return index, content

    async def _get_or_create_inflight_translation(
        self,
        cache_key: str,
        *,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        segment: str,
        source_lang: str,
        target_lang: str,
        glossary_entries: list[GlossaryEntry],
    ) -> str:
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        async with self._inflight_lock:
            cached = self._cache_get(cache_key)
            if cached is not None:
                return cached

            task = self._inflight_translations.get(cache_key)
            if task is None:
                task = asyncio.create_task(
                    self._request_segment_translation(
                        client=client,
                        semaphore=semaphore,
                        segment=segment,
                        source_lang=source_lang,
                        target_lang=target_lang,
                        glossary_entries=glossary_entries,
                        cache_key=cache_key,
                    )
                )
                self._inflight_translations[cache_key] = task

        try:
            return await task
        finally:
            async with self._inflight_lock:
                if self._inflight_translations.get(cache_key) is task:
                    self._inflight_translations.pop(cache_key, None)

    async def _request_segment_translation(
        self,
        *,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        segment: str,
        source_lang: str,
        target_lang: str,
        glossary_entries: list[GlossaryEntry],
        cache_key: str,
    ) -> str:
        async with semaphore:
            try:
                response = await client.post(
                    self._build_endpoint(),
                    headers={
                        "Authorization": f"Bearer {self._settings.openai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=self._build_payload(segment, source_lang, target_lang, glossary_entries),
                )
                response.raise_for_status()
                data = response.json()
                content = self._extract_content(data)
                self._cache_put(cache_key, content)
                return content
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text.strip()
                if not detail:
                    detail = f"upstream returned HTTP {exc.response.status_code}"
                raise RuntimeError(
                    f"Model request failed with HTTP {exc.response.status_code}: {detail}"
                ) from exc
            except httpx.HTTPError as exc:
                raise RuntimeError(f"Model request failed: {exc}") from exc

    def _build_endpoint(self) -> str:
        base = self._settings.openai_base_url.rstrip("/")
        if self._settings.openai_api_mode == "chat":
            return f"{base}/chat/completions"
        return f"{base}/completions"

    def _build_payload(
        self,
        segment: str,
        source_lang: str,
        target_lang: str,
        glossary_entries: list[GlossaryEntry],
    ) -> dict:
        language_block = self._build_language_block(source_lang, target_lang)
        if self._settings.openai_api_mode == "chat":
            return {
                "model": self._settings.openai_model,
                "temperature": self._settings.openai_temperature,
                "max_tokens": self._settings.openai_max_tokens,
                "messages": [
                    {"role": "system", "content": self._settings.openai_system_prompt},
                    {
                        "role": "user",
                        "content": (
                            f"{language_block}\n"
                            f"{self._build_glossary_block(glossary_entries)}"
                            "Return only the translation.\n\n"
                            f"{segment}"
                        ),
                    },
                ],
            }

        return {
            "model": self._settings.openai_model,
            "temperature": self._settings.openai_temperature,
            "max_tokens": self._settings.openai_max_tokens,
            "prompt": self._build_completion_prompt(
                segment,
                source_lang,
                target_lang,
                glossary_entries,
            ),
        }

    def _build_completion_prompt(
        self,
        segment: str,
        source_lang: str,
        target_lang: str,
        glossary_entries: list[GlossaryEntry],
    ) -> str:
        source_name = self._language_name(source_lang)
        target_name = self._language_name(target_lang)
        target_tag = self._language_tag(target_lang)
        if source_lang == "auto":
            task_line = (
                f"Detect the source language from the text and translate it into {target_name}. "
            )
        else:
            task_line = f"Translate the following {source_name} sentence into {target_name}. "
        return (
            f"{self._settings.openai_system_prompt}\n"
            f"{self._build_glossary_block(glossary_entries)}"
            f"{task_line}"
            "Return only the translation:\n"
            f"{segment} <{target_tag}>"
        )

    def _build_language_block(self, source_lang: str, target_lang: str) -> str:
        target_name = self._language_name(target_lang)
        if source_lang == "auto":
            return (
                "Source language: detect automatically from the input\n"
                f"Target language: {target_name}"
            )
        return (
            f"Source language: {self._language_name(source_lang)}\n"
            f"Target language: {target_name}"
        )

    def _build_glossary_block(self, glossary_entries: list[GlossaryEntry]) -> str:
        if not glossary_entries:
            return ""
        lines = [
            "Apply the following glossary exactly when the source term appears:",
        ]
        for entry in glossary_entries:
            note = f" ({entry.note})" if entry.note else ""
            lines.append(f"- {entry.source} => {entry.target}{note}")
        return "\n".join(lines) + "\n"

    def _extract_content(self, data: dict) -> str:
        choice = data["choices"][0]
        if self._settings.openai_api_mode == "chat":
            return choice["message"]["content"].strip()
        return choice["text"].strip()

    def _build_cache_key(
        self,
        segment: str,
        source_lang: str,
        target_lang: str,
        glossary_entries: list[GlossaryEntry],
    ) -> str:
        glossary_key = "||".join(
            f"{entry.source_lang}>{entry.target_lang}:{entry.source}=>{entry.target}|{entry.note}"
            for entry in glossary_entries
        )
        return "\x1f".join((source_lang, target_lang, segment, glossary_key))

    def _cache_get(self, key: str) -> str | None:
        cached = self._segment_cache.get(key)
        if cached is None:
            return None
        self._segment_cache.move_to_end(key)
        return cached

    def _cache_put(self, key: str, value: str) -> None:
        self._segment_cache[key] = value
        self._segment_cache.move_to_end(key)
        max_size = max(0, self._settings.openai_segment_cache_size)
        while len(self._segment_cache) > max_size:
            self._segment_cache.popitem(last=False)

    def _language_name(self, code: str) -> str:
        if code == "auto":
            return "Auto"
        return language_name(code)

    def _language_tag(self, code: str) -> str:
        tags = {
            "en": "en",
            "ko": "ko",
            "ja": "ja",
            "zh": "zh",
        }
        return tags.get(code, code)

    def segment_source_text(self, text: str) -> list[str]:
        return split_translation_units(text, max_chars=self._settings.openai_chunk_chars)
