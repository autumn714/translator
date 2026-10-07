"""HTTP API of the document jobs (routers/documents.py) on a minimal FastAPI app."""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from urllib.parse import unquote

import docfixtures as F
from fastapi import FastAPI
from fastapi.testclient import TestClient

from translator_app.documents.jobs import JobManager
from translator_app.routers.documents import content_disposition, router


def make_app(tmp_path, translator=None, **settings_over) -> FastAPI:
    s = F.settings(tmp_path, **settings_over)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager = JobManager(settings=s, translator=translator or F.FakeTranslator())
        await manager.start()
        app.state.job_manager = manager
        app.state.settings = s
        yield
        await manager.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    return app


def upload(client: TestClient, name: str, data: bytes, options: dict | str | None = None):
    form = {}
    if options is not None:
        form["options"] = options if isinstance(options, str) else json.dumps(options)
    return client.post("/api/documents", files={"file": (name, data, "application/octet-stream")}, data=form)


def wait(client: TestClient, job_id: str, timeout: float = 30.0) -> dict:
    end = time.monotonic() + timeout
    while True:
        r = client.get(f"/api/documents/{job_id}")
        assert r.status_code == 200
        job = r.json()
        if job["status"] in ("done", "error", "canceled"):
            return job
        if time.monotonic() > end:
            raise AssertionError(job)
        time.sleep(0.02)


def test_upload_poll_download_preview_report_delete(tmp_path):
    with TestClient(make_app(tmp_path)) as client:
        r = upload(client, "수소 안전.docx", F.make_docx(), {"source_lang": "en", "target_lang": "ko",
                                                              "formality": "formal", "output": "translated"})
        assert r.status_code == 202, r.text
        job = r.json()
        assert set(job) >= {"id", "filename", "size", "format", "source_lang", "target_lang", "status", "progress",
                            "output_filename", "warnings", "error", "created_at", "finished_at", "expires_at",
                            "eta_seconds", "chars", "report_count"}
        assert job["filename"] == "수소 안전.docx" and job["format"] == ".docx" and job["size"] > 0
        assert client.get(f"/api/documents/{job['id']}/report").status_code == 404 or job["status"] == "done"
        done = wait(client, job["id"])
        assert done["status"] == "done" and done["output_filename"] == "수소 안전_ko.docx"
        assert done["progress"]["percent"] == 100.0

        r = client.get(f"/api/documents/{job['id']}/download")
        assert r.status_code == 200 and r.content.startswith(b"PK")
        assert r.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.wordprocessingml")
        cd = r.headers["content-disposition"]
        assert cd.startswith("attachment;") and 'filename="' in cd
        assert unquote(cd.split("filename*=UTF-8''", 1)[1]) == "수소 안전_ko.docx"

        r = client.get(f"/api/documents/{job['id']}/preview")
        assert r.status_code == 200 and r.json()["text"].startswith("[KO]")
        r = client.get(f"/api/documents/{job['id']}/report")
        assert r.status_code == 200 and r.json() == {"items": []}

        r = client.get("/api/documents", params={"ids": f"{job['id']},unknown,{'f' * 32}"})
        assert r.status_code == 200 and [j["id"] for j in r.json()["jobs"]] == [job["id"]]
        assert client.get("/api/documents").json() == {"jobs": []}  # no global listing

        assert client.delete(f"/api/documents/{job['id']}").status_code == 200
        assert client.get(f"/api/documents/{job['id']}").status_code == 404
        assert client.get(f"/api/documents/{job['id']}/download").status_code == 404
        assert client.delete(f"/api/documents/{job['id']}").status_code == 404


def test_cancel_endpoint(tmp_path):
    tr = F.FakeTranslator(delay=0.5)
    with TestClient(make_app(tmp_path, tr)) as client:
        r = upload(client, "a.txt", ("Hello world.\n" * 50).encode(), {})
        job_id = r.json()["id"]
        r = client.post(f"/api/documents/{job_id}/cancel")
        assert r.status_code == 200
        assert wait(client, job_id)["status"] == "canceled"
        assert client.get(f"/api/documents/{job_id}/download").status_code == 409
        assert client.post(f"/api/documents/{'0' * 32}/cancel").status_code == 404


def test_upload_errors(tmp_path):
    with TestClient(make_app(tmp_path, doc_max_mb=1)) as client:
        r = upload(client, "big.txt", b"a" * (1024 * 1024 + 10), {})
        assert r.status_code == 413 and "1MB" in r.json()["detail"]
        assert not any((tmp_path / "data" / "jobs" / ".incoming").glob("*"))

        r = upload(client, "a.txt", b"hello", "{not json")
        assert r.status_code == 422 and r.json()["detail"] == "번역 옵션이 올바르지 않습니다."
        r = upload(client, "a.txt", b"hello", {"output": "side-by-side"})
        assert r.status_code == 422
        r = upload(client, "old.doc", b"\xd0\xcf\x11\xe0data", {})
        assert r.status_code == 415 and ".docx" in r.json()["detail"]
        r = upload(client, "empty.txt", b"", {})
        assert r.status_code == 400 and r.json()["detail"] == "빈 파일입니다."
        r = client.post("/api/documents", data={"options": "{}"})
        assert r.status_code == 400
        r = client.post("/api/documents", content=b"{}", headers={"content-type": "application/json"})
        assert r.status_code == 400
        r = client.get("/api/documents/../../etc/passwd")
        assert r.status_code == 404
        r = client.get("/api/documents/not-a-job-id")
        assert r.status_code == 404 and "찾을 수 없습니다" in r.json()["detail"]


def test_job_error_is_reported_in_korean(tmp_path):
    with TestClient(make_app(tmp_path, F.FakeTranslator("unavailable"))) as client:
        r = upload(client, "a.docx", F.make_docx(), {})
        job = wait(client, r.json()["id"])
        assert job["status"] == "error" and "모델 서버에 연결할 수 없습니다" in job["error"]
        assert client.get(f"/api/documents/{job['id']}/preview").status_code == 409


def test_unavailable_without_manager(tmp_path):
    app = FastAPI()
    app.include_router(router)
    app.state.job_manager = None
    with TestClient(app) as client:
        r = client.get("/api/documents", params={"ids": "x"})
        assert r.status_code == 503 and "문서 번역" in r.json()["detail"]


def test_content_disposition():
    cd = content_disposition("보고서 v2.docx")
    assert cd.startswith('attachment; filename="')
    ascii_part = cd.split('filename="', 1)[1].split('"', 1)[0]
    assert ascii_part.isascii() and ascii_part.endswith(".docx")
    assert unquote(cd.split("filename*=UTF-8''", 1)[1]) == "보고서 v2.docx"
    cd = content_disposition('evil"\r\nX-Injected: 1.txt')
    assert "\r" not in cd and "\n" not in cd and cd.count('"') == 2


def test_image_upload_is_refused_without_vision(tmp_path):
    from types import SimpleNamespace

    app = make_app(tmp_path)
    with TestClient(app) as client:
        manager = app.state.job_manager
        png = F.make_png("IMAGE TEXT", (300, 150))
        app.state.llm = SimpleNamespace(vision_enabled=False)
        for name in ("scan.png", "PHOTO.JPG", "a.jpeg", "b.webp"):
            r = upload(client, name, png + b"\0" * 300_000, {"source_lang": "en", "target_lang": "ko"})
            assert r.status_code == 415, (name, r.text)
            assert r.json()["detail"] == "이미지 인식을 지원하지 않는 모델입니다. 이미지 파일은 번역할 수 없습니다."
        assert not manager.jobs and not any(manager.incoming.iterdir())       # nothing kept on disk
        assert upload(client, "a.txt", b"Hello world.", {"source_lang": "en"}).status_code == 202
        app.state.llm = SimpleNamespace(vision_enabled=True)
        r = upload(client, "scan.png", png, {"source_lang": "en", "target_lang": "ko"})
        assert r.status_code == 202, r.text
