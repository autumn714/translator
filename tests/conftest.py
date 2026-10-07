from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from translator_app.config import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class IsolatedSettings(Settings):
    """Settings from keyword arguments only (the developer's environment cannot leak into tests)."""

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings,  # noqa: ANN001
                                   file_secret_settings):  # noqa: ANN001, ANN206
        return (init_settings,)


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "engine_type": "mock",
        "data_dir": tmp_path / "data",
        "llm_base_url": "http://llm.test/v1",
        "llm_model": "",
        "session_secret": "00" * 32,
    }
    values.update(overrides)
    return IsolatedSettings(**values)


@pytest.fixture
def settings_factory(tmp_path: Path) -> Callable[..., Settings]:
    def factory(**overrides: Any) -> Settings:
        return make_settings(tmp_path, **overrides)

    return factory


@pytest.fixture
def mock_client(tmp_path: Path) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    from translator_app.main import create_app

    with TestClient(create_app(make_settings(tmp_path))) as client:
        yield client


# ---------------------------------------------------------------- fake OpenAI server for httpx.MockTransport
class SSEStream(httpx.AsyncByteStream):
    """Server-sent events body; records whether the client closed it early."""

    def __init__(self, chunks: list[bytes], delay: float = 0.0) -> None:
        self.chunks = chunks
        self.delay = delay
        self.closed = False
        self.sent = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.sent += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def sse_chunks(pieces: list[str], finish_reason: str = "stop", model: str = "Fake-Qwen") -> list[bytes]:
    out = []
    for piece in pieces:
        out.append(b"data: " + json.dumps({"model": model, "choices": [
            {"index": 0, "delta": {"content": piece}, "finish_reason": None}]}).encode() + b"\n\n")
    out.append(b"data: " + json.dumps({"model": model, "choices": [
        {"index": 0, "delta": {}, "finish_reason": finish_reason}]}).encode() + b"\n\n")
    out.append(b"data: " + json.dumps({"model": model, "choices": [], "usage": {"completion_tokens": 3}}).encode()
               + b"\n\n")
    out.append(b"data: [DONE]\n\n")
    return out


def completion(content: str, finish_reason: str = "stop", model: str = "Fake-Qwen", **message: Any) -> dict[str, Any]:
    return {
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content, **message},
                     "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


class FakeOpenAI:
    """Programmable OpenAI-compatible server. ``chat`` receives the parsed body and returns
    a dict (JSON completion), an httpx.Response, or a list of SSE chunks / SSEStream for streams."""

    def __init__(self, chat: Callable[[dict[str, Any]], Any] | None = None, *, model: str = "Fake-Qwen",
                 max_model_len: int = 32768, metrics: str = "") -> None:
        self.chat = chat or (lambda body: completion("번역"))
        self.model = model
        self.max_model_len = max_model_len
        self.metrics = metrics
        self.bodies: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self.streams: list[SSEStream] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        if path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": self.model, "object": "model", "max_model_len": self.max_model_len}]})
        if path.endswith("/metrics"):
            if not self.metrics:
                return httpx.Response(404)
            return httpx.Response(200, text=self.metrics)
        if path.endswith("/health"):
            return httpx.Response(200)
        if path.endswith("/chat/completions"):
            body = json.loads(request.content)
            self.bodies.append(body)
            result = self.chat(body)
            if asyncio.iscoroutine(result):
                result = await result
            if isinstance(result, httpx.Response):
                return result
            if isinstance(result, httpx.AsyncByteStream):
                self.streams.append(result)  # type: ignore[arg-type]
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=result)
            if isinstance(result, list):
                stream = SSEStream(result)
                self.streams.append(stream)
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
            return httpx.Response(200, json=result)
        return httpx.Response(404, json={"detail": "Not Found"})


def user_text(body: dict[str, Any]) -> str:
    content = body["messages"][-1]["content"]
    if isinstance(content, str):
        return content
    return "\n".join(part.get("text", "") for part in content if part.get("type") == "text")


def batch_items(body: dict[str, Any]) -> list[str]:
    return [item["text"] for item in json.loads(user_text(body))["items"]]


def batch_answer(outputs: list[str], ids: list[int] | None = None) -> dict[str, Any]:
    ids = list(range(len(outputs))) if ids is None else ids
    payload = {"translations": [{"id": i, "output": o} for i, o in zip(ids, outputs, strict=True)]}
    return completion(json.dumps(payload, ensure_ascii=False))


@pytest.fixture
def fake_llm_factory(settings_factory: Callable[..., Settings]):
    """Build (LLMClient, FakeOpenAI) with retries/backoff disabled for speed."""
    from translator_app.llm.client import LLMClient

    clients: list[LLMClient] = []

    def factory(chat: Callable[[dict[str, Any]], Any] | None = None, **kwargs: Any):
        settings_kwargs = kwargs.pop("settings", {})
        fake = FakeOpenAI(chat, **kwargs)
        settings = settings_factory(engine_type="openai_compatible", **settings_kwargs)
        client = LLMClient(settings, transport=httpx.MockTransport(fake))
        client.retry_backoff = (0.0, 0.0)
        clients.append(client)
        return client, fake

    return factory
