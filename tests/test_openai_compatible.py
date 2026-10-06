import asyncio

import httpx
import pytest

from conftest import IsolatedSettings
from translator_app.engines.factory import build_engine
from translator_app.engines.openai_compatible import OpenAICompatibleEngine
from translator_app.llm.client import LLMClient
from translator_app.schemas import TranslateOptions
from translator_app.services.glossary import GlossaryStore
from translator_app.services.translator import TranslatorService


@pytest.mark.anyio
async def test_openai_engine_reuses_inflight_segment_requests(tmp_path) -> None:
    settings = IsolatedSettings(  # the pre-v2 OPENAI_* names are still accepted
        engine_type="openai_compatible",
        data_dir=tmp_path / "data",
        OPENAI_BASE_URL="http://example.test/v1",
        OPENAI_MODEL="test-model",
        OPENAI_PARALLELISM=4,
        OPENAI_SEGMENT_CACHE_SIZE=16,
    )
    assert settings.llm_base_url == "http://example.test/v1" and settings.llm_model == "test-model"
    assert settings.llm_max_parallel == 4 and settings.segment_cache_size == 16
    request_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        if not request.url.path.endswith("/chat/completions"):
            return httpx.Response(404)
        request_count += 1
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"choices": [{"message": {"content": "translated"}, "finish_reason": "stop"}]})

    llm = LLMClient(settings, transport=httpx.MockTransport(handler))
    engine = build_engine(settings, llm)
    assert isinstance(engine, OpenAICompatibleEngine)
    service = TranslatorService(settings, llm, engine, GlossaryStore(settings.glossary_file))
    try:
        result = await service.translate_text("same\n\nsame", TranslateOptions(source_lang="en", target_lang="ko"))
        legacy = await engine.translate("other", "en", "ko", [])
    finally:
        await engine.aclose()
        await llm.aclose()

    assert result.translation == "translated\n\ntranslated"
    assert [segment.target for segment in result.segments] == ["translated", "translated"]
    assert request_count == 2  # one for the deduplicated pair, one for the legacy call
    assert legacy.translation == "translated"
