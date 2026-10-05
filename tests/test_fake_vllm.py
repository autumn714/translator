"""dev/fake_vllm.py (개발·시험용 가짜 vLLM 서버) 시험."""
from __future__ import annotations

import importlib.util
import json
import re
import threading
from pathlib import Path

import httpx
import pytest

FAKE_PATH = Path(__file__).resolve().parents[1] / "dev" / "fake_vllm.py"


def load_fake():
    spec = importlib.util.spec_from_file_location("fake_vllm", FAKE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


fake = load_fake()


@pytest.fixture()
def server_factory():
    servers = []

    def start(*extra: str) -> str:
        srv = fake.make_server(fake.parse_args(["--port", "0", *extra]))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        host, port = srv.server_address[:2]
        return f"http://{host}:{port}"

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def base(server_factory) -> str:
    return server_factory()


def chat(base: str, **body) -> httpx.Response:
    body.setdefault("model", "Fake-Qwen")
    return httpx.post(f"{base}/v1/chat/completions", json=body, timeout=10)


def test_health_models_metrics(base):
    assert httpx.get(f"{base}/health").status_code == 200
    models = httpx.get(f"{base}/v1/models").json()
    assert models["data"][0] == {**models["data"][0], "id": "Fake-Qwen", "max_model_len": 32768, "root": "fake"}
    metrics = httpx.get(f"{base}/metrics").text
    assert re.search(r'^vllm:num_requests_running\{[^}]*model_name="Fake-Qwen"[^}]*\} 0\.0$', metrics, re.M)
    assert re.search(r'^vllm:num_requests_waiting\{[^}]*\} 0\.0$', metrics, re.M)
    assert httpx.get(f"{base}/nope").status_code == 404


def test_plain_translation_uses_target_line_and_keeps_tags(base):
    r = chat(base, messages=[
        {"role": "system", "content": "Translate.\nTarget language: Korean (ko)"},
        {"role": "user", "content": "Hello <g1>world</g1><x2/>"},
    ])
    assert r.status_code == 200
    data = r.json()
    assert data["choices"][0]["message"]["content"] == "[KO] Hello <g1>world</g1><x2/>"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"]["completion_tokens"] > 0


def test_structured_output_matches_array_length(base):
    items = ["First <g1>bold</g1> line", "Second line", "Third<x1/>"]
    schema = {
        "type": "object",
        "properties": {"translations": {"type": "array", "items": {"type": "string"},
                                        "minItems": 3, "maxItems": 3}},
        "required": ["translations"],
    }
    user = "Translate each item.\n" + json.dumps({"items": items}, ensure_ascii=False)
    r = chat(base, messages=[{"role": "user", "content": user}],
             response_format={"type": "json_schema", "json_schema": {"name": "t", "schema": schema}})
    out = json.loads(r.json()["choices"][0]["message"]["content"])
    assert out == {"translations": [f"[KO] {s}" for s in items]}


def test_structured_outputs_field_and_fallback_length(base):
    schema = {"type": "object", "properties": {"alternatives": {
        "type": "array", "items": {"type": "string"}, "minItems": 4, "maxItems": 4}}}
    r = chat(base, messages=[{"role": "user", "content": "no json here"}], structured_outputs={"json": schema})
    out = json.loads(r.json()["choices"][0]["message"]["content"])
    assert len(out["alternatives"]) == 4
    assert len(set(out["alternatives"])) == 4


def test_structured_object_items_with_ref(base):
    schema = {
        "$defs": {"Item": {"type": "object", "properties": {"id": {"type": "integer"}, "text": {"type": "string"}}}},
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"$ref": "#/$defs/Item"}, "minItems": 2, "maxItems": 2}},
    }
    user = 'Translate: [{"id": 0, "text": "alpha"}, {"id": 1, "text": "beta"}]'
    r = chat(base, messages=[{"role": "user", "content": user}],
             response_format={"type": "json_schema", "json_schema": {"name": "t", "schema": schema}})
    out = json.loads(r.json()["choices"][0]["message"]["content"])
    assert out == {"items": [{"id": 0, "text": "[KO] alpha"}, {"id": 1, "text": "[KO] beta"}]}


def test_image_part_returns_fake_ocr(base):
    r = chat(base, messages=[{"role": "user", "content": [
        {"type": "text", "text": "Read this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}])
    assert r.json()["choices"][0]["message"]["content"] == fake.OCR_TEXT


def test_stream_with_usage(base):
    body = {"model": "Fake-Qwen", "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "Target language: ja\nGood morning everyone"}]}
    events = []
    with httpx.stream("POST", f"{base}/v1/chat/completions", json=body, timeout=10) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(line[6:])
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text == "[JA] Target language: ja\nGood morning everyone"
    assert [c for c in chunks if c["choices"]][-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["choices"] == [] and chunks[-1]["usage"]["completion_tokens"] > 0


def test_max_tokens_truncates(base):
    r = chat(base, max_tokens=2, messages=[{"role": "user", "content": "one two three four five"}])
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "length"
    assert choice["message"]["content"] == "[KO] one "


def test_n_choices_are_distinct(base):
    r = chat(base, n=3, messages=[{"role": "user", "content": "hello"}])
    contents = [c["message"]["content"] for c in r.json()["choices"]]
    assert len(contents) == 3 and len(set(contents)) == 3


def test_errors(server_factory):
    base = server_factory("--fail-rate", "1")
    r = chat(base, messages=[{"role": "user", "content": "x"}])
    assert r.status_code == 500
    r = chat(base, model="other-model", messages=[{"role": "user", "content": "x"}])
    assert r.status_code == 404 and "does not exist" in r.json()["message"]
    r = httpx.post(f"{base}/v1/chat/completions", content=b"not json", timeout=10)
    assert r.status_code == 400
    stats = httpx.get(f"{base}/fake/stats").json()
    assert stats["requests"] == 2 and stats["failures"] == 1


def test_think_prefix(server_factory):
    base = server_factory("--think")
    r = chat(base, messages=[{"role": "user", "content": "hi"}])
    assert r.json()["choices"][0]["message"]["content"].startswith("<think>")
