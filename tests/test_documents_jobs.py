"""JobManager lifecycle: queue, progress, cancel, delete, restart recovery, expiry."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import docfixtures as F
import pytest

from translator_app.documents.base import MSG_INTERRUPTED
from translator_app.documents.jobs import JobManager, _chunks, _lanes


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _many_paragraphs(n: int) -> bytes:
    return F.make_minimal_docx([f"Paragraph number {i} about hydrogen storage safety." for i in range(n)])


async def _wait(cond, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


@pytest.mark.anyio
async def test_lifecycle_progress_download_delete(tmp_path):
    tr = F.FakeTranslator()
    tr.gate = asyncio.Event()
    m = JobManager(settings=F.settings(tmp_path, llm_doc_parallel=1), translator=tr)
    await m.start()
    try:
        job = await m.submit(_many_paragraphs(120), "long.docx", {"source_lang": "en", "target_lang": "ko"})
        assert len(job.id) == 32 and job.status in ("queued", "extracting", "translating")
        d = m.job_dir(job.id)
        assert (d / "input.docx").is_file()
        meta = json.loads((d / "meta.json").read_text("utf-8"))
        assert meta["filename"] == "long.docx" and meta["status"] in ("queued", "extracting", "translating")
        await _wait(lambda: job.status == "translating" and tr.calls)
        pub = job.public()
        assert pub.progress.total == 121 and pub.progress.done == 0 and pub.progress.percent == 0.0
        tr.gate.set()
        await F.wait_done(job)
        assert job.status == "done", job.error
        pub = job.public()
        assert pub.progress.done == pub.progress.total == 121 and pub.progress.percent == 100.0
        assert pub.output_filename == "long_ko.docx" and pub.chars and pub.finished_at and pub.eta_seconds is None
        assert pub.expires_at > pub.created_at
        # several requests in document order, each with the previous translations as context
        assert len(tr.calls) > 1
        assert tr.calls[0]["preceding"] is None
        prev = tr.calls[1]["preceding"]
        assert prev and len(prev) == 3 and prev[-1][1] == "[KO] " + prev[-1][0]
        flat = [t for c in tr.calls for t in c["texts"]]
        assert flat[:2] == ["Paragraph number 0 about hydrogen storage safety.",
                            "Paragraph number 1 about hydrogen storage safety."]
        meta = json.loads((d / "meta.json").read_text("utf-8"))
        assert meta["status"] == "done" and meta["output_file"] == "output.docx"
        assert (d / "preview.txt").read_text("utf-8").startswith("[KO] Paragraph number 0")
        assert json.loads((d / "report.json").read_text("utf-8")) == {"items": []}
        assert m.output_path(job) == d / "output.docx"
        assert [j.id for j in m.list([job.id, "nope", job.id, "0" * 32])] == [job.id]
        assert await m.delete(job.id)
        assert not d.exists() and m.get(job.id) is None
        assert not await m.delete(job.id)
    finally:
        await m.aclose()


@pytest.mark.anyio
async def test_eta_and_parallel_lanes(tmp_path):
    tr = F.FakeTranslator(delay=0.4)                               # ~8 requests in two lanes ≈ 1.6 s
    m = JobManager(settings=F.settings(tmp_path, llm_doc_parallel=2), translator=tr)
    await m.start()
    try:
        job = await m.submit(_many_paragraphs(300), "big.docx", {"source_lang": "en"})
        etas = []
        while job.status not in ("done", "error"):
            if job.status == "translating":
                etas.append(job.public().eta_seconds)
            await asyncio.sleep(0.02)
        assert job.status == "done", job.error
        assert any(e is not None for e in etas)
        # two lanes: the second lane starts in the middle of the document without context
        starts = [c for c in tr.calls if c["preceding"] is None]
        assert len(starts) == 2
    finally:
        await m.aclose()


@pytest.mark.anyio
async def test_cancel_running_and_queued(tmp_path):
    tr = F.FakeTranslator()
    tr.gate = asyncio.Event()                                      # holds every request
    m = JobManager(settings=F.settings(tmp_path, doc_job_concurrency=1), translator=tr)
    await m.start()
    try:
        first = await m.submit(_many_paragraphs(10), "a.docx", {})
        second = await m.submit(_many_paragraphs(10), "b.docx", {})
        await _wait(lambda: first.status == "translating")
        assert second.status == "queued"
        await m.cancel(second.id)
        assert second.status == "canceled"
        await m.cancel(first.id)
        await F.wait_done(first)
        assert first.status == "canceled" and first.error is None
        assert not list(m.job_dir(first.id).glob("output*"))
        meta = json.loads((m.job_dir(first.id) / "meta.json").read_text("utf-8"))
        assert meta["status"] == "canceled"
        # the worker is free again
        tr.gate.set()
        third = await m.submit(_many_paragraphs(3), "c.docx", {})
        await F.wait_done(third)
        assert third.status == "done"
    finally:
        await m.aclose()


@pytest.mark.anyio
async def test_delete_running_job_removes_files(tmp_path):
    tr = F.FakeTranslator()
    tr.gate = asyncio.Event()
    m = JobManager(settings=F.settings(tmp_path), translator=tr)
    await m.start()
    try:
        job = await m.submit(_many_paragraphs(5), "a.docx", {})
        await _wait(lambda: job.status == "translating")
        assert await m.delete(job.id)
        await _wait(lambda: not m.job_dir(job.id).exists())
        assert m.get(job.id) is None
    finally:
        await m.aclose()


@pytest.mark.anyio
async def test_restart_recovery_and_expiry(tmp_path):
    s = F.settings(tmp_path)
    tr = F.FakeTranslator()
    m = JobManager(settings=s, translator=tr)
    await m.start()
    done, _ = await F.run_job(m, "done.docx", _many_paragraphs(2))
    assert done.status == "done"
    tr.gate = asyncio.Event()
    running = await m.submit(_many_paragraphs(2), "running.docx", {})
    await _wait(lambda: running.status == "translating")
    await m.aclose()                                               # server stops mid-translation
    assert running.status == "error" and running.error == MSG_INTERRUPTED

    # a job left "translating" on disk by a crash is marked interrupted on start
    meta_path = m.job_dir(running.id) / "meta.json"
    meta = json.loads(meta_path.read_text("utf-8"))
    meta.update(status="translating", error=None)
    meta_path.write_text(json.dumps(meta), "utf-8")
    # an expired finished job is deleted on start
    expired, _ = done, None
    m2 = JobManager(settings=s, translator=F.FakeTranslator())
    await m2.start()
    try:
        again = m2.get(running.id)
        assert again is not None and again.status == "error" and again.error == MSG_INTERRUPTED
        kept = m2.get(done.id)
        assert kept is not None and kept.status == "done" and m2.output_path(kept) is not None
        assert m2.preview_text(kept).startswith("[KO]")
        # expiry: move the finished job into the past and run the cleanup
        kept.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert await m2.cleanup_expired() == 1
        assert m2.get(done.id) is None and not m2.job_dir(done.id).exists()
        assert expired is done
    finally:
        await m2.aclose()


@pytest.mark.anyio
async def test_expired_on_disk_removed_at_start(tmp_path):
    s = F.settings(tmp_path)
    m = JobManager(settings=s, translator=F.FakeTranslator())
    await m.start()
    job, _ = await F.run_job(m, "old.docx", _many_paragraphs(1))
    await m.aclose()
    meta_path = m.job_dir(job.id) / "meta.json"
    meta = json.loads(meta_path.read_text("utf-8"))
    meta["expires_at"] = "2000-01-01T00:00:00Z"
    meta_path.write_text(json.dumps(meta), "utf-8")
    m2 = JobManager(settings=s, translator=F.FakeTranslator())
    await m2.start()
    try:
        assert m2.get(job.id) is None and not m2.job_dir(job.id).exists()
    finally:
        await m2.aclose()


@pytest.mark.anyio
async def test_limits(tmp_path):
    m = JobManager(settings=F.settings(tmp_path, doc_max_chars=100, doc_max_mb=1), translator=F.FakeTranslator())
    await m.start()
    try:
        job, _ = await F.run_job(m, "long.docx", _many_paragraphs(10))
        assert job.status == "error" and "너무 깁니다" in job.error
        from translator_app.documents.base import DocumentError

        with pytest.raises(DocumentError) as e:
            await m.submit(b"x" * (1024 * 1024 + 1), "big.txt", {})
        assert e.value.status_code == 413
        with pytest.raises(DocumentError):
            await m.submit(b"", "empty.txt", {})
    finally:
        await m.aclose()


@pytest.mark.anyio
async def test_submit_moves_upload_and_sanitizes_name(tmp_path):
    m = JobManager(settings=F.settings(tmp_path), translator=F.FakeTranslator())
    await m.start()
    try:
        part = m.new_upload_path()
        part.write_bytes(b"Hello world\n")
        job = await m.submit(part, "..\\..\\보고서\x00<1>.txt", {"output": "bilingual"})
        assert not part.exists()
        assert job.filename == "보고서_1_.txt"
        await F.wait_done(job)
        assert job.output_filename.startswith("보고서_1__") and job.output_filename.endswith("-ko.txt")
        # bilingual is ignored for formats that cannot hold it
        job2 = await m.submit(b"<p>Hello world</p>", "a.html", {"output": "bilingual"})
        assert job2.options["output"] == "translated"
    finally:
        await m.aclose()


@pytest.mark.anyio
async def test_glossary_report(tmp_path):
    m = JobManager(settings=F.settings(tmp_path), translator=F.FakeTranslator())
    await m.start()
    try:
        opts = {"source_lang": "en", "target_lang": "ko",
                "glossary_entries": [{"source": "electrolyzer", "target": "수전해 장치"}]}
        job, _ = await F.run_job(m, "a.docx", F.make_docx(), **opts)
        assert job.status == "done"
        items = m.report_items(job)
        assert job.report_count == len(items) > 0
        assert any(i["issue"].startswith("용어집 미적용: electrolyzer") for i in items)
    finally:
        await m.aclose()


def test_chunking_and_lanes():
    texts = [f"t{i}" * 50 for i in range(100)]                 # 100-150 chars each
    chunks = _chunks(texts)
    assert sum(len(c) for c in chunks) == 100 and all(len(c) <= 30 for c in chunks)
    assert [t for c in chunks for t in c] == texts
    lanes = _lanes(chunks, 2)
    assert len(lanes) == 2 and [t for lane in lanes for c in lane for t in c] == texts
    assert _lanes(chunks[:1], 4) == [chunks[:1]]


@pytest.mark.anyio
async def test_preceding_context_is_plain_text_even_when_tags_were_dropped(tmp_path):
    tr = F.FakeTranslator(mode="drop_tags")                        # keeps the encoder's "&amp;", drops the tags
    m = JobManager(settings=F.settings(tmp_path, llm_doc_parallel=1), translator=tr)
    await m.start()
    try:
        paragraphs = "".join(f"<p><b>R&amp;D</b> budget number {i} grew.</p>" for i in range(40))
        html = f"<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"></head><body>{paragraphs}</body></html>"
        job, _ = await F.run_job(m, "rd.html", html.encode("utf-8"))
        assert job.status == "done", job.error
        assert len(tr.calls) > 1 and "&amp;" in tr.calls[0]["texts"][0]
        prev = tr.calls[1]["preceding"]
        assert prev and all("&amp;" not in s and "&amp;" not in o for s, o in prev)
        assert prev[-1][1].startswith("[KO] R&D budget number")
    finally:
        await m.aclose()
