"""Regression tests for the core review fixes (disconnects, detection, segmentation, cache, glossary,
images, request limits, login, CSRF, status)."""
from __future__ import annotations

import asyncio
import io
import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import make_settings
from translator_app.auth import COOKIE_NAME, LoginThrottle, is_exempt
from translator_app.engines.mock import MockEngine
from translator_app.main import create_app
from translator_app.schemas import GlossaryEntry, TranslateOptions
from translator_app.services.glossary import GlossaryStore, match_glossary_entries, normalize_language_code
from translator_app.services.language_detection import detect_source_languages
from translator_app.services.segmentation import join_units, split_units
from translator_app.services.translator import TranslatorService

TRADITIONAL = "這是一個繁體中文的句子，我們用來測試語言偵測。燃料電池將氫氣轉換為電能。"
SIMPLIFIED = "这是一个简体中文的句子，我们用来测试语言检测。燃料电池将氢气转换为电能。"


# ---------------------------------------------------------------- raw ASGI helpers
def http_scope(method: str, path: str, headers: list[tuple[bytes, bytes]] | None = None) -> dict[str, Any]:
    return {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"testserver"), *(headers or [])], "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }


def response_status(messages: list[dict[str, Any]]) -> int | None:
    for message in messages:
        if message["type"] == "http.response.start":
            return message["status"]
    return None


def response_json(messages: list[dict[str, Any]]) -> Any:
    return json.loads(b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"))


# ---------------------------------------------------------------- core-01: client disconnect cancels the work
@pytest.mark.anyio
@pytest.mark.parametrize(("path", "payload", "method"), [
    ("/api/translate", {"text": "Hello world.", "source_lang": "en", "target_lang": "ko"}, "translate_unit"),
    ("/api/rewrite", {"text": "This are wrong.", "lang": "en", "style": "polish"}, "rewrite"),
    ("/api/alternatives", {"source": "Hello.", "translation": "안녕하세요.", "span": "안녕하세요",
                           "source_lang": "en", "target_lang": "ko"}, "alternatives"),
    ("/api/lookup", {"term": "hydrogen", "source_lang": "en", "target_lang": "ko"}, "lookup"),
])
async def test_non_stream_endpoints_are_cancelled_when_the_client_leaves(tmp_path, path, payload, method) -> None:
    app = create_app(make_settings(tmp_path))
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def slow(*args: Any, **kwargs: Any) -> Any:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    setattr(app.state.translator.engine, method, slow)
    body = json.dumps(payload).encode()
    gone = asyncio.Event()
    state = {"sent": False}

    async def receive() -> dict[str, Any]:
        if not state["sent"]:
            state["sent"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    task = asyncio.create_task(app(http_scope("POST", path, headers), receive, send))
    await asyncio.wait_for(started.wait(), 5)
    gone.set()
    await asyncio.wait_for(cancelled.wait(), 5)  # the upstream call is cancelled, not left running
    await asyncio.wait_for(task, 5)
    assert response_status(messages) != 200


# ---------------------------------------------------------------- core-02: Traditional Chinese
def test_traditional_chinese_is_detected_as_zh_hant() -> None:
    assert detect_source_languages(TRADITIONAL).primary_language == "zh-Hant"
    assert detect_source_languages(SIMPLIFIED).primary_language == "zh-Hans"


def test_auto_detected_traditional_text_is_converted_not_returned_unchanged(mock_client) -> None:
    data = mock_client.post("/api/translate", json={"text": TRADITIONAL, "source_lang": "auto",
                                                    "target_lang": "zh-Hans"}).json()
    assert data["detected_source_lang"] == "zh-Hant"
    assert data["translation"].startswith("[zh-Hans] ")
    same = mock_client.post("/api/translate", json={"text": SIMPLIFIED, "source_lang": "auto",
                                                    "target_lang": "zh-Hans"}).json()
    assert same["translation"] == SIMPLIFIED  # identity shortcut still works
    mixed_script = SIMPLIFIED + "這個"  # mostly Simplified with Traditional characters: let the model convert
    converted = mock_client.post("/api/translate", json={"text": mixed_script, "source_lang": "auto",
                                                         "target_lang": "zh-Hans"}).json()
    assert converted["translation"].startswith("[zh-Hans] ")
    rewrite = mock_client.post("/api/rewrite", json={"text": TRADITIONAL, "lang": "auto", "style": "polish"}).json()
    assert rewrite["detected_lang"] == "zh-Hant"


# ---------------------------------------------------------------- core-03: CJK sentence ends
def test_long_chinese_and_japanese_paragraphs_split_at_sentence_ends() -> None:
    chinese = "氢能是一种清洁的二次能源，具有广泛的应用前景和重要的战略意义。" * 60
    units = split_units(chinese, max_chars=1200)
    assert len(units) > 1 and all(unit.text.endswith("。") for unit in units)
    assert join_units(units, [unit.text for unit in units], joiner="") == chinese
    japanese = "「これは日本語の文です。」と彼は言った。次の文もあります！本当ですか？" * 40
    units = split_units(japanese, max_chars=300)
    assert len(units) > 1
    assert all(unit.text[-1] in "。！？」" for unit in units)
    assert not any(unit.text.startswith(("」", "？")) for unit in units)
    assert join_units(units, [unit.text for unit in units], joiner="") == japanese


def test_latin_sentences_still_need_whitespace_after_the_period() -> None:
    text = ("Version 3.5 of the tool. " * 60).strip()
    units = split_units(text, max_chars=200)
    assert all(unit.text.endswith("tool.") for unit in units)


# ---------------------------------------------------------------- core-04: empty / cut-off outputs
class ScriptedEngine(MockEngine):
    def __init__(self, outputs: list[tuple[str, str]]) -> None:
        self.outputs = list(outputs)
        self.calls: list[bool] = []

    async def translate_unit(self, text: str, spec: Any, *, priority: str = "interactive", tags: bool = False,
                             strict_tags: str | None = None, retry: bool = False,
                             meta: dict[str, Any] | None = None) -> str:
        self.calls.append(retry)
        value, finish = self.outputs.pop(0) if self.outputs else (f"[ok] {text}", "stop")
        if meta is not None:
            meta["finish_reason"] = finish
        return value


def scripted_service(tmp_path, outputs: list[tuple[str, str]]) -> tuple[TranslatorService, ScriptedEngine]:
    settings = make_settings(tmp_path)
    engine = ScriptedEngine(outputs)
    return TranslatorService(settings, None, engine, GlossaryStore(settings.glossary_file)), engine


OPTS = TranslateOptions(source_lang="en", target_lang="ko")


@pytest.mark.anyio
async def test_empty_unit_is_retried_once_and_good_result_cached(tmp_path) -> None:
    service, engine = scripted_service(tmp_path, [("", "stop"), ("번역", "stop")])
    assert (await service.translate_text("Hello there.", OPTS)).translation == "번역"
    assert engine.calls == [False, True]
    assert (await service.translate_text("Hello there.", OPTS)).translation == "번역"
    assert engine.calls == [False, True]  # cached


@pytest.mark.anyio
async def test_empty_or_truncated_results_are_not_cached(tmp_path) -> None:
    service, engine = scripted_service(tmp_path, [("", "stop"), ("", "stop")])
    assert (await service.translate_text("Hello there.", OPTS)).translation == ""
    assert (await service.translate_text("Hello there.", OPTS)).translation == "[ok] Hello there."
    service, engine = scripted_service(tmp_path, [("잘린 번", "length")])
    assert (await service.translate_text("Hello again.", OPTS)).translation == "잘린 번"
    assert (await service.translate_text("Hello again.", OPTS)).translation == "[ok] Hello again."
    events = [event async for event in service.stream_text("Hello again.", OPTS)]
    assert events[-1]["translation"] == "[ok] Hello again."


# ---------------------------------------------------------------- core-05: language codes
def test_language_codes_are_normalized_case_insensitively() -> None:
    cases = {"EN": "en", "en-US": "en", "ko_KR": "ko", "KO": "ko", "zh-hans": "zh-Hans", "ZH-HANT": "zh-Hant",
             "zh-TW": "zh-Hant", "zh-Hant-TW": "zh-Hant", "zh-CN": "zh-Hans", "zh": "zh-Hans", "pt-BR": "pt",
             "auto": "auto", "AUTO": "auto", "xx": "xx", " Fil ": "fil"}
    for raw, expected in cases.items():
        assert normalize_language_code(raw) == expected, raw


def test_glossary_import_and_translate_accept_uppercase_codes(mock_client) -> None:
    csv = "source_lang,target_lang,source,target\nEN,KO,hydrogen,수소\nen-US,ko-KR,fuel cell,연료전지\n"
    response = mock_client.post("/api/glossaries/default/import", data={"mode": "replace"},
                                files={"file": ("terms.csv", csv.encode("utf-8"), "text/csv")})
    assert response.status_code == 200, response.text
    entries = response.json()["glossary"]["entries"]
    assert {(e["source_lang"], e["target_lang"]) for e in entries} == {("en", "ko")}
    translated = mock_client.post("/api/translate", json={"text": "Hello.", "source_lang": "EN", "target_lang": "KO"})
    assert translated.status_code == 200 and translated.json()["translation"] == "[ko] Hello."


# ---------------------------------------------------------------- core-07: detection off the event loop
@pytest.mark.anyio
async def test_language_detection_runs_in_a_worker_thread(tmp_path, monkeypatch) -> None:
    import translator_app.services.translator as translator_module

    threads: list[int] = []
    original = translator_module.detect_source_languages

    def recording(text: str):  # noqa: ANN202
        threads.append(threading.get_ident())
        return original(text)

    monkeypatch.setattr(translator_module, "detect_source_languages", recording)
    service, _ = scripted_service(tmp_path, [])
    await service.translate_text("Hello there, this is English.", TranslateOptions(source_lang="auto"))
    assert threads and threading.get_ident() not in threads


# ---------------------------------------------------------------- C8: explicit source language
def test_explicit_source_language_is_reported_as_given(mock_client) -> None:
    data = mock_client.post("/api/translate", json={
        "text": "Fuel cells convert hydrogen into electricity.", "source_lang": "en", "target_lang": "ko"}).json()
    assert data["source_language_mode"] == "single" and data["primary_source_lang"] == "en"
    assert [item["code"] for item in data["detected_source_languages"]] == ["en"]
    assert data["detected_source_lang"] == "en"


# ---------------------------------------------------------------- core-08: whole-word glossary matching
def test_short_latin_terms_match_whole_words_only() -> None:
    entries = [GlossaryEntry(source="AI", target="인공지능"), GlossaryEntry(source="IT", target="정보기술"),
               GlossaryEntry(source="PM", target="프로젝트 관리자"), GlossaryEntry(source="fuel cell", target="연료전지"),
               GlossaryEntry(source="수소", target="hydrogen", source_lang="ko", target_lang="en")]

    def hits(text: str, target: str = "ko", source: str = "en") -> set[str]:
        return {e.source for e in match_glossary_entries(text, entries, target_lang=target, source_lang=source)}

    assert hits("We maintain the plant with equipment and it works.") == set()
    assert hits("AI and IT teams; the PMs met.") == {"AI", "IT", "PM"}
    assert hits("Fuel cells and a fuel\ncell stack.") == {"fuel cell"}
    assert hits("수소를 생산한다.", target="en", source="ko") == {"수소"}


# ---------------------------------------------------------------- EXIF-rotated photos
def _jpeg(size: tuple[int, int], orientation: int) -> bytes:
    from PIL import Image

    image = Image.new("RGB", size, (200, 200, 200))
    exif = Image.Exif()
    exif[0x0112] = orientation
    out = io.BytesIO()
    image.save(out, format="JPEG", exif=exif.tobytes())
    return out.getvalue()


def test_rotated_photos_keep_their_aspect_ratio() -> None:
    from PIL import Image

    from translator_app.llm.client import prepare_image

    data, mime = prepare_image(_jpeg((2400, 1800), 6), "image/jpeg")  # portrait photo stored landscape
    width, height = Image.open(io.BytesIO(data)).size
    assert mime == "image/jpeg" and height > width and abs(height / width - 4 / 3) < 0.01
    data, _ = prepare_image(_jpeg((400, 300), 6), "image/jpeg")  # small: no pass-through without rotation
    assert Image.open(io.BytesIO(data)).size == (300, 400)
    upright = _jpeg((400, 300), 1)
    assert prepare_image(upright, "image/jpeg") == (upright, "image/jpeg")


# ---------------------------------------------------------------- chunked request bodies
@pytest.mark.anyio
@pytest.mark.parametrize(("path", "content_type", "max_reads"), [
    ("/api/translate", b"application/json", 6),
    ("/api/glossaries/default/import", b"multipart/form-data; boundary=xyz", 8),
])
async def test_chunked_bodies_are_cut_off_at_the_limit(tmp_path, path, content_type, max_reads) -> None:
    app = create_app(make_settings(tmp_path))
    reads = 0
    head = (b'--xyz\r\nContent-Disposition: form-data; name="file"; filename="a.csv"\r\n'
            b"Content-Type: text/csv\r\n\r\n") if content_type.startswith(b"multipart") else b""

    async def receive() -> dict[str, Any]:
        nonlocal reads
        reads += 1
        if reads > 50:
            raise AssertionError("the body was read without limit")
        return {"type": "http.request", "body": (head if reads == 1 else b"") + b"x" * (1024 * 1024),
                "more_body": True}

    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await asyncio.wait_for(app(http_scope("POST", path, [(b"content-type", content_type)]), receive, send), 20)
    assert response_status(messages) == 413
    assert response_json(messages) == {"detail": "요청이 너무 큽니다."}
    assert reads <= max_reads


# ---------------------------------------------------------------- login: sessions, static exemption
def _auth_app(tmp_path, password: str = "old-pass"):  # noqa: ANN202
    app = create_app(make_settings(tmp_path, ui_auth="1", ui_user="keei", ui_password=password))
    app.state.login_throttle = LoginThrottle(delay=0.01)
    return app


def _login_token(app) -> str:  # noqa: ANN001
    with TestClient(app, follow_redirects=False) as client:
        response = client.post("/api/login", json={"username": "keei", "password": "old-pass"})
        assert response.status_code == 200
        return response.cookies[COOKIE_NAME].strip('"')


def _status_with(app, token: str) -> int:  # noqa: ANN001
    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set(COOKIE_NAME, token)
        return client.get("/api/status").status_code


def test_password_change_invalidates_existing_cookies(tmp_path) -> None:
    app = _auth_app(tmp_path)
    token = _login_token(app)
    assert _status_with(app, token) == 200
    assert _status_with(_auth_app(tmp_path, password="new-pass"), token) == 401


def test_restart_invalidates_cookies_so_logout_survives_it(tmp_path) -> None:
    app = _auth_app(tmp_path)
    with TestClient(app, follow_redirects=False) as client:
        token = client.post("/api/login", json={"username": "keei", "password": "old-pass"}).cookies[COOKIE_NAME]
        assert client.post("/api/logout").status_code == 200
    token = token.strip('"')
    assert _status_with(app, token) == 401  # logged out
    restarted = _auth_app(tmp_path)  # same password, revocation list lost: the copied cookie still fails
    assert _status_with(restarted, token) == 401
    assert _status_with(restarted, _login_token(_auth_app(tmp_path))) == 401  # any pre-restart cookie
    assert _status_with(restarted, _login_token(restarted)) == 200


def test_logout_revokes_the_session_on_the_server(tmp_path) -> None:
    app = _auth_app(tmp_path)
    with TestClient(app, follow_redirects=False) as client:
        token = client.post("/api/login", json={"username": "keei", "password": "old-pass"}).cookies[COOKIE_NAME]
        assert client.get("/api/status").status_code == 200
        assert client.post("/api/logout").status_code == 200
        client.cookies.set(COOKIE_NAME, token.strip('"'))  # a copied cookie
        assert client.get("/api/status").status_code == 401
        assert client.post("/api/login", json={"username": "keei", "password": "old-pass"}).status_code == 200
        assert client.get("/api/status").status_code == 200


def test_wrong_logins_are_slowed_down_but_the_right_one_is_not(tmp_path) -> None:
    app = create_app(make_settings(tmp_path, ui_auth="1", ui_user="keei", ui_password="old-pass"))
    app.state.login_throttle = LoginThrottle(delay=0.3)
    with TestClient(app, follow_redirects=False) as client:
        started = time.perf_counter()
        assert client.post("/api/login", json={"username": "keei", "password": "bad"}).status_code == 401
        assert time.perf_counter() - started >= 0.29
        for _ in range(3):
            client.post("/api/login", json={"username": "keei", "password": "bad"})
        started = time.perf_counter()
        assert client.post("/api/login", json={"username": "keei", "password": "old-pass"}).status_code == 200
        assert time.perf_counter() - started < 0.25


@pytest.mark.anyio
async def test_static_login_exemption_cannot_climb_out(tmp_path) -> None:
    assert is_exempt("/static/login.html") and is_exempt("/static/login.css")
    for path in ("/static/login./../index.html", "/static/login./../js/api.js", "/static/login.x/y"):
        assert not is_exempt(path), path
    app = _auth_app(tmp_path)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    for path, expected in (("/static/login./../index.html", 302), ("/static/login.html", 200)):
        messages: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)  # noqa: B023

        await app(http_scope("GET", path), receive, send)
        assert response_status(messages) == expected, path


# ---------------------------------------------------------------- C1: UI_AUTH values
@pytest.mark.parametrize(("raw", "expected"), [
    ("1", True), ("true", True), (" TRUE ", True), ("Yes", True), ("on", True), ("ON", True),
    ("0", False), ("false", False), ("off", False), ("", False), ("2", False), ("enabled", False), ("y", False),
])
def test_ui_auth_truthy_values(tmp_path, raw: str, expected: bool) -> None:
    assert make_settings(tmp_path, ui_auth=raw).ui_auth is expected


def test_disk_quota_setting(tmp_path) -> None:
    assert make_settings(tmp_path).doc_disk_quota_mb == 2048
    assert make_settings(tmp_path, doc_disk_quota_mb="").doc_disk_quota_mb == 2048
    assert make_settings(tmp_path, doc_disk_quota_mb="100").doc_disk_quota_mb == 100


# ---------------------------------------------------------------- C5: cross-site requests
def test_cross_site_state_changes_are_refused(mock_client) -> None:
    csv = b"source,target\nhydrogen,suso\n"

    def upload(headers: dict[str, str]) -> int:
        return mock_client.post("/api/glossaries/default/import", data={"mode": "replace"}, headers=headers,
                                files={"file": ("evil.csv", csv, "text/csv")}).status_code

    assert upload({"Origin": "http://evil.example"}) == 403
    assert upload({"Origin": "null"}) == 403
    assert upload({"Sec-Fetch-Site": "cross-site"}) == 403
    assert upload({"Origin": "http://testserver:8080"}) == 403  # another port on the same host
    refused = mock_client.post("/api/translate", json={"text": "hi"}, headers={"Origin": "http://evil.example"})
    assert refused.status_code == 403 and refused.json() == {"detail": "허용되지 않은 요청입니다."}
    assert upload({"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}) == 200
    assert upload({}) == 200  # curl / scripts send no Origin
    assert mock_client.get("/api/glossaries", headers={"Origin": "http://evil.example"}).status_code == 200


def test_configured_cors_origins_may_post(tmp_path) -> None:
    app = create_app(make_settings(tmp_path, cors_allow_origins="http://partner.example"))
    with TestClient(app) as client:
        response = client.post("/api/translate", json={"text": "hi", "source_lang": "en"},
                               headers={"Origin": "http://partner.example", "Sec-Fetch-Site": "cross-site"})
        assert response.status_code == 200


# ---------------------------------------------------------------- C4: document formats and fallback
def test_image_formats_are_hidden_without_vision() -> None:
    from translator_app.routers.system import document_formats

    def formats(vision: bool) -> set[str]:
        state = SimpleNamespace(job_manager=object(), llm=SimpleNamespace(vision_enabled=vision))
        return {item["ext"] for item in document_formats(SimpleNamespace(app=SimpleNamespace(state=state)))}

    with_vision, without = formats(True), formats(False)
    assert {".png", ".jpg"} <= with_vision
    assert not ({".png", ".jpg", ".jpeg", ".webp"} & without) and ".docx" in without


def test_documents_api_reports_503_when_the_subsystem_is_missing(tmp_path, monkeypatch) -> None:
    import translator_app.main as main_module

    monkeypatch.setattr(main_module, "_load_documents", lambda settings, translator: (None, None))
    with TestClient(main_module.create_app(make_settings(tmp_path))) as client:
        assert client.get("/api/status").json()["document_formats"] == []
        for method, path in (("GET", "/api/documents?ids=a"), ("POST", "/api/documents"),
                             ("GET", "/api/documents/abc/download"), ("DELETE", "/api/documents/abc")):
            response = client.request(method, path)
            assert response.status_code == 503, path
            assert response.json() == {"detail": "문서 번역 기능을 사용할 수 없습니다."}
