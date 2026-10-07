"""LLMClient: exact request bodies (SPEC §6a), parsing, retries, status."""
from __future__ import annotations

import asyncio
import io
import json
import time

import httpx
import pytest

from conftest import SSEStream, completion, sse_chunks
from translator_app.llm.client import (
    JSON_SAMPLING,
    REWRITE_SAMPLING,
    TRANSLATE_SAMPLING,
    LLMClient,
    LLMError,
    LLMOutputError,
    LLMUnavailable,
    VisionUnavailable,
    completion_budget,
    estimate_tokens,
    image_data_uri,
    parse_metrics,
    prepare_image,
    strip_think,
)

pytestmark = pytest.mark.anyio

MESSAGES = [{"role": "system", "content": "rules"}, {"role": "user", "content": "Hello world"}]
SAMPLING_KEYS = {"temperature", "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty"}


def assert_common_body(body: dict) -> None:
    assert body["chat_template_kwargs"]["enable_thinking"] is False
    assert body["repetition_detection"] == {"min_pattern_size": 8, "max_pattern_size": 64, "min_count": 4}
    assert SAMPLING_KEYS <= body.keys()
    assert isinstance(body["max_completion_tokens"], int) and body["max_completion_tokens"] >= 64
    for forbidden in ("reasoning_effort", "priority", "max_tokens", "n", "guided_json"):
        assert forbidden not in body
    assert not any(key.startswith("guided_") for key in body)
    if not body.get("stream"):
        assert "stream_options" not in body


async def test_chat_body_has_every_required_field(fake_llm_factory) -> None:
    client, fake = fake_llm_factory()
    try:
        result = await client.chat(MESSAGES, sampling=TRANSLATE_SAMPLING, max_tokens=500)
    finally:
        await client.aclose()
    assert result.content == "번역"
    body = fake.bodies[0]
    assert_common_body(body)
    assert body["model"] == "Fake-Qwen"  # discovered via GET /models
    assert body["messages"] == MESSAGES
    assert {k: body[k] for k in SAMPLING_KEYS} == {
        "temperature": 0.2, "top_p": 0.8, "top_k": 20, "min_p": 0.0, "presence_penalty": 0.0,
        "repetition_penalty": 1.0,
    }
    assert body["max_completion_tokens"] == 500
    assert "stream" not in body and "response_format" not in body
    assert client.model_name == "Fake-Qwen" and client.max_model_len == 32768


async def test_extra_body_is_merged_but_cannot_override_protected_keys(fake_llm_factory) -> None:
    extra = {"seed": 7, "max_tokens": 5, "stream_options": {"include_usage": True}, "reasoning_effort": "low",
             "chat_template_kwargs": {"foo": 1, "enable_thinking": True}, "priority": 3}
    client, fake = fake_llm_factory(settings={"llm_extra_body": json.dumps(extra), "llm_model": "Fake-Qwen"})
    try:
        await client.chat(MESSAGES, sampling=REWRITE_SAMPLING, max_tokens=300)
    finally:
        await client.aclose()
    body = fake.bodies[0]
    assert_common_body(body)
    assert body["seed"] == 7
    assert body["chat_template_kwargs"] == {"enable_thinking": False, "foo": 1}
    assert body["temperature"] == 0.3 and body["top_k"] == 20


async def test_stream_body_and_deltas(fake_llm_factory) -> None:
    client, fake = fake_llm_factory(lambda body: sse_chunks(["<think>", "\n\n</think>", "\n\n안녕", "하세요"]))
    meta: dict = {}
    try:
        pieces = [piece async for piece in client.chat_stream(MESSAGES, sampling=TRANSLATE_SAMPLING, max_tokens=300,
                                                              meta=meta)]
    finally:
        await client.aclose()
    assert "".join(pieces) == "안녕하세요"
    assert meta["finish_reason"] == "stop" and meta["usage"] == {"completion_tokens": 3}
    body = fake.bodies[0]
    assert_common_body(body)
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}


async def test_closing_the_stream_closes_the_upstream_response(fake_llm_factory) -> None:
    stream = SSEStream(sse_chunks([f"t{i} " for i in range(50)]), delay=0.01)
    client, fake = fake_llm_factory(lambda body: stream)
    try:
        agen = client.chat_stream(MESSAGES, sampling=TRANSLATE_SAMPLING, max_tokens=300)
        first = await agen.__anext__()
        assert first.startswith("t0")
        await agen.aclose()
        assert stream.closed
        assert stream.sent < 50
        assert client.limiter.active == 0  # slot released
    finally:
        await client.aclose()


async def test_chat_json_uses_response_format_json_schema(fake_llm_factory) -> None:
    schema = {"type": "object", "properties": {"a": {"type": "array", "items": {"type": "string"},
                                                     "minItems": 2, "maxItems": 2}},
              "required": ["a"], "additionalProperties": False}
    client, fake = fake_llm_factory(lambda body: completion('```json\n{"a": ["x", "y"]}\n```'))
    try:
        value = await client.chat_json(MESSAGES, schema=schema, name="pair", max_tokens=256)
    finally:
        await client.aclose()
    assert value == {"a": ["x", "y"]}
    body = fake.bodies[0]
    assert_common_body(body)
    assert body["response_format"] == {"type": "json_schema",
                                       "json_schema": {"name": "pair", "schema": schema, "strict": True}}
    assert {k: body[k] for k in ("temperature", "top_p", "top_k")} == {"temperature": 0.0, "top_p": 1.0, "top_k": -1}


async def test_chat_json_rejects_wrong_shape(fake_llm_factory) -> None:
    schema = {"type": "object", "properties": {"a": {"type": "array", "minItems": 2, "maxItems": 2}},
              "required": ["a"]}
    client, _ = fake_llm_factory(lambda body: completion('{"a": ["only one"]}'))
    try:
        with pytest.raises(LLMOutputError):
            await client.chat_json(MESSAGES, schema=schema, name="pair", max_tokens=256, sampling=JSON_SAMPLING)
    finally:
        await client.aclose()


async def test_truncated_output_is_retried_once_with_presence_penalty(fake_llm_factory) -> None:
    answers = iter([completion("loop loop", finish_reason="repetition"), completion("좋은 번역")])
    client, fake = fake_llm_factory(lambda body: next(answers))
    try:
        result = await client.chat(MESSAGES, sampling=TRANSLATE_SAMPLING, max_tokens=300)
    finally:
        await client.aclose()
    assert result.content == "좋은 번역"
    assert [b["presence_penalty"] for b in fake.bodies] == [0.0, 1.0]


async def test_reasoning_without_content_is_a_configuration_error(fake_llm_factory) -> None:
    client, _ = fake_llm_factory(lambda body: completion("", reasoning="thinking..."))
    try:
        with pytest.raises(LLMError, match="생각 모드"):
            await client.chat(MESSAGES, sampling=TRANSLATE_SAMPLING, max_tokens=300)
    finally:
        await client.aclose()


async def test_busy_server_is_retried_then_reported_in_korean(fake_llm_factory) -> None:
    client, fake = fake_llm_factory(lambda body: httpx.Response(503, json={"message": "busy"}))
    try:
        with pytest.raises(LLMUnavailable, match="바쁩니다"):
            await client.chat(MESSAGES, sampling=TRANSLATE_SAMPLING, max_tokens=300)
    finally:
        await client.aclose()
    assert len(fake.bodies) == 3  # 1 + 2 retries


async def test_connection_error_retries_then_unavailable(settings_factory) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path.endswith("/chat/completions"):
            calls += 1
        raise httpx.ConnectError("refused", request=request)

    client = LLMClient(settings_factory(engine_type="openai_compatible", llm_model="m"),
                       transport=httpx.MockTransport(handler))
    client.retry_backoff = (0.0, 0.0)
    try:
        with pytest.raises(LLMUnavailable, match="연결할 수 없습니다"):
            await client.chat(MESSAGES, sampling=TRANSLATE_SAMPLING, max_tokens=300)
    finally:
        await client.aclose()
    assert calls == 3  # 1 + 2 retries


async def test_model_rediscovered_after_404(fake_llm_factory) -> None:
    def chat(body: dict) -> object:
        if body["model"] == "Old":
            return httpx.Response(404, json={"message": "The model `Old` does not exist."})
        return completion("ok")

    client, fake = fake_llm_factory(chat, model="New")
    client._model = "Old"  # e.g. the server switched to the FP8 build
    try:
        result = await client.chat(MESSAGES, sampling=TRANSLATE_SAMPLING, max_tokens=300)
    finally:
        await client.aclose()
    assert result.content == "ok"
    assert [b["model"] for b in fake.bodies] == ["Old", "New"]


async def test_image_rejection_maps_to_vision_unavailable(fake_llm_factory) -> None:
    client, _ = fake_llm_factory(lambda body: httpx.Response(400, json={"message": "image input is not supported"}))
    messages = [{"role": "user", "content": [{"type": "text", "text": "read"},
                                             {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}]
    try:
        with pytest.raises(VisionUnavailable):
            await client.chat(messages, sampling=TRANSLATE_SAMPLING, max_tokens=300)
        assert client.vision_enabled is False
    finally:
        await client.aclose()


async def test_status_reports_load_and_never_raises(fake_llm_factory, settings_factory) -> None:
    metrics = (
        '# HELP vllm:num_requests_running x\n'
        'vllm:num_requests_running{engine="0",model_name="Fake-Qwen"} 2.0\n'
        'vllm:num_requests_running{engine="1",model_name="Fake-Qwen"} 1.0\n'
        'vllm:num_requests_waiting{engine="0",model_name="Fake-Qwen"} 4.0\n'
        'vllm:num_requests_waiting_by_reason{reason="capacity"} 9.0\n'
    )
    client, _ = fake_llm_factory(metrics=metrics)
    try:
        status = await client.status(force=True)
    finally:
        await client.aclose()
    assert status == {"connected": True, "name": "Fake-Qwen", "max_model_len": 32768, "vision": True,
                      "running": 3, "waiting": 4, "error": None}

    down = LLMClient(settings_factory(engine_type="openai_compatible", llm_base_url="http://127.0.0.1:9/v1"))
    started = time.monotonic()
    try:
        status = await down.status(force=True)
    finally:
        await down.aclose()
    assert status["connected"] is False and status["error"]
    assert time.monotonic() - started < 3.0


def test_parse_metrics_sums_label_variants_and_ignores_by_reason() -> None:
    values = parse_metrics(
        'vllm:num_requests_waiting{a="1"} 1\nvllm:num_requests_waiting{a="2"} 2\n'
        'vllm:num_requests_waiting_by_reason{reason="x"} 7\nvllm:kv_cache_usage_perc{a="1"} 0.25\n'
    )
    assert values == {"waiting": 3.0, "kv_cache": 0.25}


def test_strip_think_variants() -> None:
    assert strip_think("<think>\nplan\n</think>\n\n번역") == "번역"
    assert strip_think("</think>\n번역") == "번역"
    assert strip_think("  번역  ") == "번역"
    assert strip_think(None) == ""


def test_completion_budget_sizes_to_input() -> None:
    assert completion_budget("") == 256
    assert estimate_tokens("가" * 100) == 50
    assert completion_budget("a" * 400) == 3 * 100 + 256
    assert completion_budget("가" * 100000) == 8192


def test_prepare_image_downscales_large_images() -> None:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4000, 3000), (255, 255, 255)).save(buffer, format="PNG")
    data, mime = prepare_image(buffer.getvalue(), "image/png")
    with Image.open(io.BytesIO(data)) as img:
        assert img.width * img.height <= 2_400_000 and max(img.size) <= 2000
    assert mime == "image/png"

    small = io.BytesIO()
    Image.new("RGBA", (50, 40), (0, 0, 0, 0)).save(small, format="WEBP")
    uri = image_data_uri(small.getvalue(), "image/webp")
    assert uri.startswith("data:image/png;base64,")
    with pytest.raises(ValueError):
        prepare_image(b"not an image", "image/png")


async def test_load_poller_sets_congestion(fake_llm_factory) -> None:
    gate = asyncio.Event()

    async def slow_chat(body: dict) -> dict:
        await gate.wait()
        return completion("ok")

    client, _ = fake_llm_factory(slow_chat, metrics='vllm:num_requests_waiting{a="1"} 2\n'
                                                      'vllm:num_requests_running{a="1"} 8\n')
    client.poll_interval = 0.01
    try:
        task = asyncio.create_task(client.chat(MESSAGES, sampling=TRANSLATE_SAMPLING, max_tokens=300))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if client.limiter.congested:
                break
        assert client.limiter.congested and client.limiter.capacity == 1
        gate.set()
        assert (await task).content == "ok"
    finally:
        await client.aclose()
    assert client.limiter.congested is False
