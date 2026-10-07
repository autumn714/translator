"""HTTP API with ENGINE_TYPE=mock (SPEC §7)."""
from __future__ import annotations

import io
import json
import time
import zipfile

from fastapi.testclient import TestClient

from conftest import make_settings
from translator_app.main import create_app


def ndjson(response) -> list[dict]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_health_and_status(mock_client) -> None:
    assert mock_client.get("/health").json() == {"status": "ok"}
    status = mock_client.get("/api/status").json()
    assert status["app_version"] == "2.0.0" and status["engine"] == "mock"
    assert status["model"]["connected"] is True
    assert status["auth"] == {"enabled": False, "user": None}
    assert status["limits"] == {"text_max_chars": 30000, "doc_max_mb": 50, "doc_retention_hours": 24}
    assert isinstance(status["document_formats"], list)


def test_translate_keeps_the_response_shape(mock_client) -> None:
    response = mock_client.post("/api/translate", json={
        "text": "The hydrogen plant is large.\n\nIt runs every day.", "source_lang": "auto", "target_lang": "ko",
        "formality": "formal"})
    assert response.status_code == 200
    data = response.json()
    assert data["translation"] == "[ko] The hydrogen plant is large.\n\n[ko] It runs every day."
    assert data["segments"][0] == {"source": "The hydrogen plant is large.",
                                   "target": "[ko] The hydrogen plant is large."}
    for key in ("glossary_hits", "glossary_applied", "glossary_name", "detected_source_languages",
                "primary_source_lang", "source_language_mode", "detected_source_lang", "engine", "latency_ms"):
        assert key in data
    assert data["formality"] == "formal" and data["engine"] == "mock"


def test_translate_validation_errors_are_korean(mock_client) -> None:
    too_long = mock_client.post("/api/translate", json={"text": "a" * 30001, "target_lang": "ko"})
    assert too_long.status_code == 413 and "30,000" in too_long.json()["detail"]
    bad_lang = mock_client.post("/api/translate", json={"text": "hi", "target_lang": "xx"})
    assert bad_lang.status_code == 400 and "언어" in bad_lang.json()["detail"]
    bad_glossary = mock_client.post("/api/translate", json={"text": "hi", "target_lang": "ko",
                                                            "glossary_id": "nope"})
    assert bad_glossary.status_code == 404 and bad_glossary.json()["detail"] == "선택한 용어집을 찾을 수 없습니다."
    invalid = mock_client.post("/api/translate", json={"text": "hi", "formality": "rude"})
    assert invalid.status_code == 422 and isinstance(invalid.json()["detail"], str)
    stream_404 = mock_client.post("/api/translate/stream", json={"text": "hi", "glossary_id": "nope"})
    assert stream_404.status_code == 404


def test_stream_is_ndjson(mock_client) -> None:
    response = mock_client.post("/api/translate/stream", json={
        "text": "Hello world.\n\nSecond paragraph.", "source_lang": "en", "target_lang": "de"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = ndjson(response)
    assert events[0]["type"] == "start" and events[0]["units"] == 2
    assert events[-1]["type"] == "done"
    assert events[-1]["translation"] == "[de] Hello world.\n\n[de] Second paragraph."
    assert {e["type"] for e in events[1:-1]} == {"delta", "unit"}


def test_alternatives_rewrite_lookup(mock_client) -> None:
    alt = mock_client.post("/api/alternatives", json={
        "source": "The plant is big.", "translation": "[ko] The plant is big.", "span": "big", "target_lang": "ko"})
    assert alt.status_code == 200 and alt.json() == {"alternatives": ["big (1)", "big (2)", "big (3)"]}
    rewrite = mock_client.post("/api/rewrite", json={"text": "Teh plan is good.", "lang": "auto", "style": "concise",
                                                     "context": ""})
    assert rewrite.status_code == 200 and rewrite.json()["text"] == "[concise] Teh plan is good."
    lookup = mock_client.post("/api/lookup", json={"term": "plant", "context": "The plant is big.",
                                                   "source_lang": "en", "target_lang": "ko"})
    assert lookup.status_code == 200
    assert lookup.json()["term"] == "plant" and lookup.json()["entries"][0]["pos"] == "명사"
    bad_style = mock_client.post("/api/rewrite", json={"text": "x", "style": "pirate"})
    assert bad_style.status_code == 422


def test_index_injects_languages_with_formality(mock_client) -> None:
    response = mock_client.get("/")
    assert response.status_code == 200
    assert "__TRANSLATOR_LANGUAGES__" not in response.text
    assert '"formality": ["auto", "formal", "informal", "plain", "gaejoshik"]' in response.text


def test_body_size_limit(mock_client) -> None:
    response = mock_client.post("/api/translate", content=b"x" * (5 * 1024 * 1024),
                                headers={"content-type": "application/json"})
    assert response.status_code == 413 and response.json()["detail"] == "요청이 너무 큽니다."


# ---------------------------------------------------------------- glossaries
def test_glossary_crud(mock_client) -> None:
    listed = mock_client.get("/api/glossaries").json()
    assert listed["default_glossary_id"] == "default"
    created = mock_client.post("/api/glossaries", json={"name": "에너지", "entries": [
        {"source_lang": "en", "target_lang": "ko", "source": "grid", "target": "전력망"}]})
    assert created.status_code == 201
    glossary_id = created.json()["glossary"]["id"]
    updated = mock_client.put(f"/api/glossaries/{glossary_id}", json={"name": "에너지 용어", "entries": [
        {"source_lang": "en", "target_lang": "ko", "source": "grid", "target": "계통", "note": "전력"}]})
    assert updated.json()["glossary"]["entries"][0]["target"] == "계통"
    translated = mock_client.post("/api/translate", json={"text": "The grid is stable.", "source_lang": "en",
                                                          "target_lang": "ko", "glossary_id": glossary_id}).json()
    assert translated["glossary_name"] == "에너지 용어" and translated["glossary_hits"][0]["source"] == "grid"
    assert mock_client.delete(f"/api/glossaries/{glossary_id}").status_code == 204
    assert mock_client.get(f"/api/glossaries/{glossary_id}").status_code == 404
    assert mock_client.get("/api/glossaries/x").json()["detail"] == "용어집을 찾을 수 없습니다."
    bad = mock_client.post("/api/glossaries", json={"name": "x", "entries": [
        {"source_lang": "en", "target_lang": "qq", "source": "a", "target": "b"}]})
    assert bad.status_code == 400


def test_glossary_export_import_round_trip(mock_client) -> None:
    created = mock_client.post("/api/glossaries", json={"name": "수소, \"용어\"", "entries": [
        {"source_lang": "en", "target_lang": "ko", "source": "fuel cell", "target": "연료전지", "note": "a, b"},
        {"source_lang": "en", "target_lang": "ko", "source": "stack", "target": "스택", "enabled": False},
    ]}).json()["glossary"]
    exported = mock_client.get(f"/api/glossaries/{created['id']}/export?format=csv")
    assert exported.status_code == 200
    assert exported.content.startswith(b"\xef\xbb\xbfsource_lang,target_lang,source,target,note,enabled\r\n")
    assert "filename*=UTF-8''" in exported.headers["content-disposition"]
    tsv = mock_client.get(f"/api/glossaries/{created['id']}/export?format=tsv")
    assert b"\tfuel cell\t" in tsv.content

    target = mock_client.post("/api/glossaries", json={"name": "빈 용어집", "entries": []}).json()["glossary"]
    imported = mock_client.post(f"/api/glossaries/{target['id']}/import",
                                files={"file": ("terms.csv", exported.content, "text/csv")},
                                data={"mode": "replace"})
    assert imported.status_code == 200
    entries = imported.json()["glossary"]["entries"]
    assert entries == created["entries"]

    two_columns = "전해조\telectrolyzer\n수소\thydrogen\n".encode("cp949")
    appended = mock_client.post(f"/api/glossaries/{target['id']}/import",
                                files={"file": ("terms.txt", two_columns, "text/plain")},
                                data={"mode": "append", "source_lang": "ko", "target_lang": "en"})
    assert appended.status_code == 200
    entries = appended.json()["glossary"]["entries"]
    assert len(entries) == 4
    assert entries[2] == {"source_lang": "ko", "target_lang": "en", "source": "전해조", "target": "electrolyzer",
                          "note": "", "enabled": True}

    broken = mock_client.post(f"/api/glossaries/{target['id']}/import",
                              files={"file": ("terms.csv", "source,target\nonly-source,\n".encode(), "text/csv")})
    assert broken.status_code == 400 and "2행" in broken.json()["detail"]


def test_glossary_import_xlsx(mock_client) -> None:
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/'
        'main"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2" t="inlineStr"><is><t>수소</t></is></c></row>'
        "</sheetData></worksheet>"
    )
    shared = (
        '<?xml version="1.0" encoding="UTF-8"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<si><t>원문</t></si><si><t>번역</t></si><si><t>hydrogen</t></si></sst>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        archive.writestr("xl/sharedStrings.xml", shared)
    imported = mock_client.post("/api/glossaries/default/import",
                                files={"file": ("terms.xlsx", buffer.getvalue(), "application/octet-stream")},
                                data={"mode": "replace", "source_lang": "en", "target_lang": "ko"})
    assert imported.status_code == 200, imported.text
    assert imported.json()["glossary"]["entries"] == [
        {"source_lang": "en", "target_lang": "ko", "source": "hydrogen", "target": "수소", "note": "", "enabled": True}]


def test_app_starts_without_reachable_llm(tmp_path) -> None:
    settings = make_settings(tmp_path, engine_type="openai_compatible", llm_base_url="http://127.0.0.1:9/v1")
    with TestClient(create_app(settings)) as client:
        client.app.state.llm.retry_backoff = (0.0, 0.0)
        started = time.monotonic()
        status = client.get("/api/status").json()
        assert time.monotonic() - started < 3.0
        assert status["model"]["connected"] is False and status["model"]["error"]
        response = client.post("/api/translate", json={"text": "Hello there.", "source_lang": "en",
                                                       "target_lang": "ko"})
        assert response.status_code == 503 and "연결" in response.json()["detail"]
        events = ndjson(client.post("/api/translate/stream", json={"text": "Hello there.", "source_lang": "en"}))
        assert events[0]["type"] == "start" and events[-1]["type"] == "error"
