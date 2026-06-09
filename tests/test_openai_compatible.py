import asyncio

import httpx
import pytest

from translator_app.config import Settings
from translator_app.engines.openai_compatible import OpenAICompatibleEngine


@pytest.mark.anyio
async def test_openai_engine_reuses_inflight_segment_requests() -> None:
    settings = Settings(
        OPENAI_BASE_URL="http://example.test/v1",
        OPENAI_MODEL="test-model",
        OPENAI_PARALLELISM=4,
        OPENAI_CHUNK_CHARS=4,
        OPENAI_SEGMENT_CACHE_SIZE=16,
    )
    engine = OpenAICompatibleEngine(settings)
    request_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        await asyncio.sleep(0.01)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "translated"}}]},
        )

    await engine._client.aclose()
    engine._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0)

    try:
        result = await engine.translate("same\n\nsame", "en", "ko", [])
    finally:
        await engine.aclose()

    assert result.translation == "translated\n\ntranslated"
    assert result.segments == ["translated", "translated"]
    assert request_count == 1
