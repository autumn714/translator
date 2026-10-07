"""translator_app.selftest: PDF check (fake and real model), the --probe load numbers, the UI_AUTH flag rule."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pymupdf
import pytest

from translator_app import selftest as st


def _pdf(lines: list[str]) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    for i, line in enumerate(lines):
        page.insert_htmlbox(pymupdf.Rect(72, 80 + 28 * i, 520, 104 + 28 * i), line)
    data = doc.tobytes()
    doc.close()
    return data


def test_check_pdf_accepts_fake_and_real_translations():
    assert st.check_pdf(_pdf([st.FAKE_KO + line for line in st.DOC_LINES])) is None
    assert st.check_pdf(_pdf(["수소 안전 수칙", "수소 용기는 환기가 잘 되는 곳에 보관한다.", "사용 전마다 압력계를 확인한다."])) is None


V030_METRICS = """\
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="Qwen3.8-27B"} 2.0
# HELP vllm:num_requests_waiting Number of requests waiting to be processed.
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{engine="0",model_name="Qwen3.8-27B"} 3.0
# HELP vllm:num_requests_waiting_by_reason Number of waiting requests by reason.
# TYPE vllm:num_requests_waiting_by_reason gauge
vllm:num_requests_waiting_by_reason{engine="0",model_name="Qwen3.8-27B",reason="capacity"} 3.0
vllm:num_requests_waiting_by_reason{engine="0",model_name="Qwen3.8-27B",reason="deferred"} 0.0
vllm:num_requests_running_total 99
"""


def test_load_from_metrics_counts_exact_names_only():
    assert st.load_from_metrics(V030_METRICS) == {"running": 2.0, "waiting": 3.0}
    two_engines = V030_METRICS + 'vllm:num_requests_waiting{engine="1",model_name="Qwen3.8-27B"} 1.0\n'
    assert st.load_from_metrics(two_engines)["waiting"] == 4.0
    assert st.load_from_metrics("") == {"running": 0.0, "waiting": 0.0}


def test_probe_reports_real_queue_length(monkeypatch, capsys):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/v1/models":
                body = json.dumps({"data": [{"id": "Qwen3.8-27B", "max_model_len": 32768}]}).encode()
            elif self.path == "/metrics":
                body = V030_METRICS.encode()
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("LLM_BASE_URL", f"http://127.0.0.1:{srv.server_address[1]}/v1")
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.setenv("no_proxy", "*")  # PC 의 프록시 설정을 타지 않게
        monkeypatch.setenv("NO_PROXY", "*")
        assert st.probe() == 0
    finally:
        srv.shutdown()
        srv.server_close()
    line = capsys.readouterr().out
    assert "처리 중 2 · 대기 3" in line


@pytest.mark.parametrize(
    ("value", "on"),
    [("1", True), ("true", True), (" Yes ", True), ("ON", True), ("0", False), ("", False), (None, False), ("y", False)],
)
def test_flag_on_matches_run_sh(value, on):
    assert st.flag_on(value) is on


def test_check_pdf_rejects_untranslated_output():
    assert st.check_pdf(st.make_pdf()) is not None
    assert st.check_pdf(_pdf([st.FAKE_KO + st.DOC_LINES[0], st.DOC_LINES[1], st.FAKE_KO + st.DOC_LINES[2]])) is not None
    assert st.check_pdf(_pdf(["수소 안전 수칙", st.DOC_LINES[1], "사용 전마다 압력계를 확인한다."])) is not None
    assert st.check_pdf(_pdf(["Wasserstoff", "Flaschen gut belüftet lagern.", "Druck prüfen."])) is not None
    assert st.check_pdf(b"not a pdf") is not None
