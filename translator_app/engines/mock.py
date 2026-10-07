"""Deterministic engine for tests and development (ENGINE_TYPE=mock, no network).

Translation = "[{target}] " + text for every paragraph; inline tags stay untouched.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Sequence
from typing import Any

from translator_app.engines.base import TranslationEngine
from translator_app.llm.prompts import TranslationSpec

_PARAGRAPH_SEP = re.compile(r"(\n[ \t]*\n)")
_PIECES = re.compile(r"\S+\s*|\s+")


def mock_translate(text: str, target_lang: str) -> str:
    parts = _PARAGRAPH_SEP.split(text)
    out: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 1 or not part.strip():
            out.append(part)
        else:
            out.append(f"[{target_lang}] {part}")
    return "".join(out)


class MockEngine(TranslationEngine):
    name = "mock"

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
        await asyncio.sleep(0)
        if meta is not None:
            meta["finish_reason"] = "stop"
        return mock_translate(text, spec.target_lang)

    async def stream_unit(
        self,
        text: str,
        spec: TranslationSpec,
        *,
        priority: str = "interactive",
        meta: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        for piece in _PIECES.findall(mock_translate(text, spec.target_lang)):
            await asyncio.sleep(0)
            yield piece
        if meta is not None:
            meta["finish_reason"] = "stop"

    async def translate_items(
        self,
        items: Sequence[str],
        spec: TranslationSpec,
        *,
        priority: str = "document",
        tags: bool = False,
        preceding: Sequence[tuple[str, str]] | None = None,
    ) -> list[str]:
        await asyncio.sleep(0)
        return [mock_translate(item, spec.target_lang) for item in items]

    async def alternatives(self, source: str, translation: str, span: str, spec: TranslationSpec) -> list[str]:
        await asyncio.sleep(0)
        return [f"{span.strip()} ({index})" for index in range(1, 4)]

    async def rewrite(self, text: str, lang: str | None, style: str, context: str = "") -> str:
        await asyncio.sleep(0)
        return f"[{style}] {text.strip()}"

    async def lookup(self, term: str, context: str, source_lang: str, target_lang: str) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {
            "entries": [{"translation": f"[{target_lang}] {term}", "pos": "명사", "note": ""}],
            "examples": [{"source": term, "target": f"[{target_lang}] {term}"}],
        }

    async def describe_image(
        self,
        data: bytes,
        mime: str,
        spec: TranslationSpec,
        *,
        mode: str = "translate",
        priority: str = "document",
    ) -> str:
        await asyncio.sleep(0)
        if mode == "extract":
            return "이미지 텍스트"
        return f"[{spec.target_lang}] 이미지 텍스트"
