"""End-to-end through the real LLMClient against dev/fake_vllm.py (started as a subprocess)."""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from conftest import make_settings
from translator_app.engines.factory import build_engine
from translator_app.llm.client import LLMClient
from translator_app.schemas import AlternativesRequest, TranslateOptions
from translator_app.services.glossary import GlossaryStore
from translator_app.services.translator import TranslatorService

FAKE = Path(__file__).resolve().parents[1] / "dev" / "fake_vllm.py"
pytestmark = pytest.mark.skipif(not FAKE.exists(), reason="dev/fake_vllm.py 없음")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def fake_vllm_url():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(FAKE), "--host", "127.0.0.1", "--port", str(port), "--delay-ms", "2"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:  # pragma: no cover
            pytest.fail("fake vLLM did not start")
        yield f"{base}/v1"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


@pytest.mark.anyio
async def test_service_against_fake_vllm(tmp_path, fake_vllm_url) -> None:
    settings = make_settings(tmp_path, engine_type="openai_compatible", llm_base_url=fake_vllm_url)
    llm = LLMClient(settings)
    service = TranslatorService(settings, llm, build_engine(settings, llm), GlossaryStore(settings.glossary_file))
    opts = TranslateOptions(source_lang="en", target_lang="ko")
    try:
        status = await llm.status(force=True)
        assert status["connected"] is True and status["name"] == "Fake-Qwen"
        assert status["max_model_len"] == 32768 and status["waiting"] == 0

        result = await service.translate_text("Hydrogen is light.\n\nIt burns cleanly.", opts)
        assert result.translation == "[KO] Hydrogen is light.\n\n[KO] It burns cleanly."

        events = [event async for event in service.stream_text("Fuel cells make power.\n\nWater remains.", opts)]
        assert events[0]["type"] == "start" and events[-1]["type"] == "done"
        assert events[-1]["translation"] == "[KO] Fuel cells make power.\n\n[KO] Water remains."
        assert sum(1 for e in events if e["type"] == "delta") > 2  # streamed in pieces

        items = ["Store <g1>hydrogen</g1> safely.<x1/>", "2026", "Check the <g2>pressure</g2> gauge.", "Vent it."]
        out = await service.translate_batch(items, TranslateOptions(source_lang="en", target_lang="ja"), tags=True,
                                            preceding=[("Intro.", "はじめに。")])
        assert out == ["[JA] Store <g1>hydrogen</g1> safely.<x1/>", "2026",
                       "[JA] Check the <g2>pressure</g2> gauge.", "[JA] Vent it."]

        alternatives = await service.alternatives(AlternativesRequest(
            source="The plant is big.", translation="[KO] The plant is big.", span="big",
            source_lang="en", target_lang="ko"))
        assert len(alternatives) == 3 and len(set(alternatives)) == 3
    finally:
        await llm.aclose()
