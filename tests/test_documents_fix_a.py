"""Regressions for the document review findings: PDF layout (redaction, fonts, units,
scans with stamps, progress), upload limits, disk quota, private job files, HWP / zip
budgets, EXIF orientation and the HWP fixture metadata."""
from __future__ import annotations

import asyncio
import io
import json
import os
import struct
import zipfile
import zlib
from datetime import datetime, timedelta, timezone

import docfixtures as F
import pytest
from fastapi.testclient import TestClient

from translator_app.documents import hwp as H
from translator_app.documents import pdf as P
from translator_app.documents import ziputil
from translator_app.documents.base import DocumentError
from translator_app.documents.jobs import MSG_DISK_FULL, Job, JobManager

pymupdf = pytest.importorskip("pymupdf")


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ====================================================================== PDF helpers
ROWS = [("Installed capacity", "1,234.5"), ("2023", "5,678"), ("Solar generation share", "12.3%")]
KO = {"Installed capacity": "설비 용량", "Solar generation share": "태양광 발전 비중"}


def _table_pdf(path, line_height: float, size: float = 10.0):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    y = 100.0
    for label, value in ROWS:
        page.insert_text((72, y), label, fontsize=size, fontname="helv")
        page.insert_text((250, y), value, fontsize=size, fontname="helv")
        y += size * line_height
    doc.save(path)
    doc.close()
    return path


def _scan_png() -> bytes:
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (620, 877), "white")
    d = ImageDraw.Draw(im)
    for i in range(20):
        d.text((50, 40 + 40 * i), f"Scanned body text line {i} about energy statistics", fill="black")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# ====================================================================== PDF: redaction keeps neighbours
@pytest.mark.parametrize("line_height", [0.85, 1.0, 1.2])
def test_pdf_tight_table_keeps_untranslated_numbers(tmp_path, line_height):
    src = _table_pdf(tmp_path / "t.pdf", line_height)
    pages = P.analyze(src)
    assert pages[0].kind == "digital"
    assert pages[0].texts == ["Installed capacity", "Solar generation share"]   # cells stay apart
    out = tmp_path / "o.pdf"
    stats = P.write_layout(src, out, dict(KO), {}, P.load_font(None), {0})
    assert stats["units"] == 2 and stats["lost"] == 0 and stats["covered"] == 0
    with pymupdf.open(out) as d:
        text = d[0].get_text()
    for kept in ("1,234.5", "5,678", "12.3%", "2023", "설비 용량", "태양광 발전 비중"):
        assert kept in text, (kept, text)
    assert "Installed capacity" not in text and "Solar generation share" not in text


def test_mupdf_redaction_criterion_still_holds():
    """write_layout relies on MuPDF keeping a glyph that a redaction rectangle only meets
    in the outer 5 % of its box (PROTECT_INSET); a MuPDF upgrade that changes this would
    silently erase neighbouring text again."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 100), "HHHH", fontsize=10, fontname="helv")
    ch = page.get_text("rawdict")["blocks"][0]["lines"][0]["spans"][0]["chars"][0]["bbox"]
    h = ch[3] - ch[1]
    page.add_redact_annot(pymupdf.Rect(ch[0], ch[3] - P.PROTECT_INSET * h, ch[2], ch[3] + 5))
    page.add_redact_annot(pymupdf.Rect(ch[0], ch[1] - 5, ch[2], ch[1] + P.PROTECT_INSET * h))
    page.apply_redactions(images=0, graphics=0, text=0)
    assert page.get_text().strip() == "HHHH"
    page.add_redact_annot(pymupdf.Rect(ch[0] + 2, ch[1] + 0.45 * h, ch[0] + 3, ch[1] + 0.55 * h))
    page.apply_redactions(images=0, graphics=0, text=0)
    assert page.get_text().strip() == "HHH"


def test_pdf_overlapping_text_is_covered_not_erased(tmp_path):
    """Glyphs of a translated line that physically overlap text that stays (a stamp) are not
    redacted (MuPDF would take the stamp with them) but covered; the stamp stays in the file
    and the rest of the line is removed normally."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Hydrogen storage tank inspection", fontsize=12, fontname="helv")
    page.insert_text((90, 101), "0000", fontsize=12, fontname="helv", color=(1, 0, 0))
    src = tmp_path / "s.pdf"
    doc.save(src)
    out = tmp_path / "o.pdf"
    stats = P.write_layout(src, out, {"Hydrogen storage tank inspection": "수소 저장 탱크 점검"}, {},
                           P.load_font(None), {0})
    assert 0 < stats["covered"] < 10 and stats["lost"] == 0
    with pymupdf.open(out) as d:
        text = d[0].get_text()
    assert "0000" in text and "수소 저장 탱크 점검" in text
    assert "inspection" not in text                        # glyphs away from the stamp were removed


def test_pdf_duplicate_invisible_layer_does_not_block_redaction(tmp_path):
    """Digital PDFs that were OCR'd again carry an invisible copy of every line; that copy
    must not turn the page into white boxes (removing it changes nothing on screen)."""
    doc = pymupdf.open()
    page = doc.new_page()
    for i in range(5):
        line = f"Line {i} of a digital report about hydrogen."
        page.insert_text((72, 100 + 20 * i), line, fontsize=11, fontname="helv")
        page.insert_text((72, 100 + 20 * i), line, fontsize=11, fontname="helv", render_mode=3)
    src = tmp_path / "dup.pdf"
    doc.save(src)
    info = P.analyze(src)[0]
    assert info.kind == "digital" and len(info.texts) == 5
    out = tmp_path / "o.pdf"
    stats = P.write_layout(src, out, {t: "번역 " + t[:6] for t in info.texts}, {}, P.load_font(None), {0})
    assert stats["units"] == 5 and stats["covered"] == 0 and stats["lost"] == 0
    with pymupdf.open(out) as d:
        assert d[0].get_text().count("번역 Line") == 5


# ====================================================================== PDF: one font copy
def _embedded_fonts(doc) -> int:
    n = 0
    for x in range(1, doc.xref_length()):
        try:
            keys = doc.xref_get_keys(x)
        except Exception:  # noqa: BLE001
            continue
        if any(k in keys for k in ("FontFile", "FontFile2", "FontFile3")):
            n += 1
    return n


def test_pdf_font_embedded_once_per_document(tmp_path, monkeypatch):
    """Before the fix every text box embedded its own CJK font copy (~3.5 MB of RAM each,
    freed only at save).  Count the embedded font files right before saving."""
    doc = pymupdf.open()
    for p in range(6):
        page = doc.new_page()
        for k in range(15):
            page.insert_text((72, 60 + k * 40), f"Paragraph {k} on page {p} about hydrogen", fontsize=10)
    src = tmp_path / "many.pdf"
    doc.save(src)
    doc.close()
    table = {t: "청정수소 인증 제도 운영 현황 " + t for info in P.analyze(src) for t in info.texts}
    assert len(table) == 90
    seen: list[int] = []
    real_save = pymupdf.Document.save

    def counting_save(self, *a, **kw):
        seen.append(_embedded_fonts(self))
        return real_save(self, *a, **kw)

    monkeypatch.setattr(pymupdf.Document, "save", counting_save)
    stats = P.write_layout(src, tmp_path / "o.pdf", table, {0: "# 번역\n\n스캔 페이지 번역", 3: "둘째 번역"},
                           P.load_font(None), None)
    assert stats["units"] == 90 and stats["added_pages"] == 2
    assert seen and seen[0] <= 6, seen                  # not one per text box (≥ 90)
    with pymupdf.open(tmp_path / "o.pdf") as d:
        assert d.page_count == 8
        assert "청정수소 인증 제도 운영 현황 Paragraph 0 on page 0" in d[0].get_text()
        assert d[1].get_text().startswith("p.1 번역") and d[5].get_text().startswith("p.4 번역")


# ====================================================================== PDF: units
def test_pdf_units_do_not_merge_table_cells(tmp_path):
    src = _table_pdf(tmp_path / "t.pdf", 1.2)
    with pymupdf.open(src) as d:
        texts = [u.text for u in P.page_units(d[0])]
    assert "5,678 Solar generation share" not in texts
    assert texts == ["Installed capacity", "Solar generation share"]


def test_pdf_units_still_join_paragraph_lines(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(72, 72, 300, 200),
                        "Hydrogen production grew steadily over the period while costs fell sharply.",
                        fontsize=11, fontname="helv")
    page.insert_text((72, 230), "Next heading", fontsize=18, fontname="hebo")
    page.insert_text((72, 246), "Body text right under the heading.", fontsize=11, fontname="helv")
    texts = [u.text for u in P.page_units(page)]
    assert texts[0] == "Hydrogen production grew steadily over the period while costs fell sharply."
    assert "Next heading" in texts and "Body text right under the heading." in texts


# ====================================================================== PDF: scans with stamps
def test_pdf_scan_with_page_number_or_stamp_goes_to_vision(tmp_path):
    png = _scan_png()
    doc = pymupdf.open()
    for case in ("plain", "page_no", "stamp", "ocr_stamp", "background_text"):
        p = doc.new_page()
        p.insert_image(p.rect, stream=png)
        if case == "page_no":
            p.insert_text((280, 820), "- 1 -", fontsize=9)
        if case in ("stamp", "ocr_stamp"):
            p.insert_text((400, 60), "CONFIDENTIAL", fontsize=14, color=(1, 0, 0))
        if case == "ocr_stamp":
            for i in range(20):
                p.insert_text((50, 50 + 30 * i), f"Scanned body text line {i}", fontsize=10, render_mode=3)
        if case == "background_text":
            p.insert_textbox(pymupdf.Rect(50, 50, 545, 790),
                             "A designed page with a background picture and plenty of digital text. " * 12,
                             fontsize=11)
    src = tmp_path / "scan.pdf"
    doc.save(src)
    kinds = [p.kind for p in P.analyze(src)]
    assert kinds == ["scanned", "scanned", "scanned", "ocr_layer", "digital"]


def _slide_pdf(path, *texts: tuple[str, float]):
    """A slide: a full-bleed picture with white text on top."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (800, 560), (40, 90, 160)).save(buf, "PNG")
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    page.insert_image(page.rect, stream=buf.getvalue())
    y = 120.0
    for text, size in texts:
        page.insert_text((60, y), text, fontsize=size, color=(1, 1, 1))
        y += size * 1.8
    doc.save(path)
    doc.close()
    return path


def test_pdf_slide_over_full_page_picture_is_digital(tmp_path):
    slide = _slide_pdf(tmp_path / "s.pdf", ("Hydrogen Strategy 2030", 28), ("Korea Energy Economics Institute", 16))
    assert [(p.kind, p.texts) for p in P.analyze(slide)] == [
        ("digital", ["Hydrogen Strategy 2030", "Korea Energy Economics Institute"])]
    # negligible text over a picture is still a scan: page numbers, a stamp, only short codes
    for i, texts in enumerate(([("- 12 -", 9)], [("CONFIDENTIAL", 14)], [("Page 3 of 12", 9)],
                               [(" ".join(f"{chr(65 + k)}{k}" for k in range(24)), 9)])):
        assert [p.kind for p in P.analyze(_slide_pdf(tmp_path / f"n{i}.pdf", *texts))] == ["scanned"], texts


@pytest.mark.anyio
@pytest.mark.parametrize("vision", [True, False])
async def test_pdf_slide_is_translated_in_place(tmp_path, vision):
    slide = _slide_pdf(tmp_path / "s.pdf", ("Hydrogen Strategy 2030", 28), ("Korea Energy Economics Institute", 16))
    tr = F.FakeTranslator(vision=vision)
    m = JobManager(settings=F.settings(tmp_path), translator=tr)
    await m.start()
    try:
        job, out = await F.run_job(m, "slide.pdf", slide.read_bytes())
        assert job.status == "done", job.error
        assert F.pdf_text(out) == ["[KO] Hydrogen Strategy 2030\n[KO] Korea Energy Economics Institute\n"]
        assert tr.image_calls == [] and job.warnings == []
        with pymupdf.open(stream=out, filetype="pdf") as d:
            assert len(d[0].get_images()) == 1                                    # the picture stays
    finally:
        await m.aclose()


@pytest.mark.anyio
async def test_pdf_scan_without_vision_translates_its_visible_text_in_place(tmp_path):
    doc = pymupdf.open()
    for stamp in ("CONFIDENTIAL", None):
        p = doc.new_page()
        p.insert_image(p.rect, stream=_scan_png())
        if stamp:
            p.insert_text((400, 60), stamp, fontsize=14, color=(1, 0, 0))
    src = tmp_path / "scan.pdf"
    doc.save(src)
    assert [p.kind for p in P.analyze(src)] == ["scanned", "scanned"]
    m = JobManager(settings=F.settings(tmp_path), translator=F.FakeTranslator(vision=False))
    await m.start()
    try:
        job, out = await F.run_job(m, "scan.pdf", src.read_bytes())
        assert job.status == "done", job.error
        pages = F.pdf_text(out)
        assert len(pages) == 2 and "[KO] CONFIDENTIAL" in pages[0] and pages[1].strip() == ""
        assert any("1쪽" in w and "이미지 속 글자" in w for w in job.warnings)
        assert any("2쪽" in w and "번역하지 않았습니다" in w for w in job.warnings)
        assert job.public().progress.percent == 100.0
    finally:
        await m.aclose()


def test_pdf_hidden_text_on_digital_page_is_not_a_unit(tmp_path):
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_textbox(pymupdf.Rect(50, 50, 545, 300), "A normal digital paragraph with real words. " * 8, fontsize=11)
    p.insert_image(pymupdf.Rect(50, 320, 300, 500), stream=_scan_png())
    p.insert_text((60, 400), "Hidden OCR words", fontsize=10, render_mode=3)
    src = tmp_path / "m.pdf"
    doc.save(src)
    info = P.analyze(src)[0]
    assert info.kind == "digital" and not any("Hidden" in t for t in info.texts)
    out = tmp_path / "o.pdf"
    P.write_layout(src, out, {t: "번역 " + t for t in info.texts}, {}, P.load_font(None), {0})
    with pymupdf.open(out) as d:
        assert "Hidden OCR words" in d[0].get_text()      # the invisible layer was left alone


# ====================================================================== PDF: progress
@pytest.mark.anyio
async def test_pdf_progress_never_goes_back_without_vision(tmp_path):
    src = F.make_pdf(scanned=True, ocr_layer=True)
    m = JobManager(settings=F.settings(tmp_path), translator=F.FakeTranslator(vision=False, delay=0.01))
    samples: list[tuple[int, int]] = []
    real = m._persist

    def record(job, *, force=False):
        samples.append((job.chars_done, job.chars_total))
        real(job, force=force)

    m._persist = record
    await m.start()
    try:
        job, out = await F.run_job(m, "r.pdf", src)
        assert job.status == "done", job.error
        started = next(i for i, (done, _total) in enumerate(samples) if done > 0)
        ceiling = samples[started][1]
        assert all(total <= ceiling for _done, total in samples[started:]), samples
        assert "[KO] Clean Hydrogen" in F.pdf_text(out)[3]
    finally:
        await m.aclose()


def test_job_percent_is_monotonic():
    now = datetime.now(timezone.utc)
    job = Job(id="a" * 32, filename="a.pdf", size=1, ext=".pdf", options={}, created_at=now, expires_at=now,
              status="translating", chars_done=50, chars_total=100)
    assert job.percent() == 50.0
    job.chars_total = 200                                  # more work discovered
    assert job.percent() == 50.0
    job.chars_done = 150
    assert job.percent() == 75.0


# ====================================================================== upload body limits
def _router_app(tmp_path, **over):
    from test_documents_router import make_app

    return make_app(tmp_path, **over)


def _chunked(body: bytes, size: int = 64 * 1024):
    def gen():
        for i in range(0, len(body), size):
            yield body[i:i + size]
    return gen()


def _multipart(parts: list[tuple[str, str | None, bytes]], boundary: str = "XyZbOuNdArY") -> tuple[bytes, str]:
    out = io.BytesIO()
    for name, filename, data in parts:
        out.write(f"--{boundary}\r\n".encode())
        disp = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
        out.write(f"Content-Disposition: {disp}\r\n\r\n".encode())
        out.write(data + b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def test_chunked_upload_with_many_fields_is_refused(tmp_path):
    parts = [(f"f{i}", None, b"x" * 1000) for i in range(50)] + [("file", "a.txt", b"Hello world.")]
    body, ctype = _multipart(parts)
    with TestClient(_router_app(tmp_path)) as client:
        r = client.post("/api/documents", content=_chunked(body), headers={"content-type": ctype})
        assert r.status_code == 400
        assert not any((tmp_path / "data" / "jobs" / ".incoming").glob("*"))


def test_chunked_upload_total_body_is_bounded(tmp_path):
    # 6 x 60 KB of other fields + a file of exactly DOC_MAX_MB: each part is allowed, the sum is not
    parts = [("pad", None, b"x" * (60 * 1024)) for _ in range(6)] + [("file", "a.txt", b"a" * (1024 * 1024))]
    body, ctype = _multipart(parts)
    with TestClient(_router_app(tmp_path, doc_max_mb=1)) as client:
        r = client.post("/api/documents", content=_chunked(body), headers={"content-type": ctype})
        assert r.status_code == 413
        # a normal chunked upload still works and unknown fields are ignored
        body, ctype = _multipart([("note", None, b"ignored"), ("options", None, b'{"target_lang": "ko"}'),
                                  ("file", "a.txt", b"Hello world.")])
        r = client.post("/api/documents", content=_chunked(body), headers={"content-type": ctype})
        assert r.status_code == 202, r.text
        assert r.json()["target_lang"] == "ko"


def test_part_header_size_is_bounded(tmp_path):
    body = (b"--B\r\nContent-Disposition: form-data; name=\"x\"; filename=\"" + b"a" * 20000 + b"\"\r\n\r\n"
            b"data\r\n--B--\r\n")
    with TestClient(_router_app(tmp_path)) as client:
        r = client.post("/api/documents", content=_chunked(body), headers={"content-type": "multipart/form-data; boundary=B"})
        assert r.status_code == 400


# ====================================================================== disk quota (C6)
@pytest.mark.anyio
async def test_disk_quota_on_submit(tmp_path):
    m = JobManager(settings=F.settings(tmp_path, doc_disk_quota_mb=1), translator=F.FakeTranslator())
    await m.start()
    try:
        first = await m.submit(b"Hello world.\n" * 45_000, "a.txt", {})          # ≈ 585 KB
        with pytest.raises(DocumentError) as e:
            await m.submit(b"Hello world.\n" * 45_000, "b.txt", {})
        assert e.value.status_code == 507 and e.value.message == MSG_DISK_FULL
        await F.wait_done(first)
        assert await m.delete(first.id)
        again = await m.submit(b"Hello world.\n" * 45_000, "c.txt", {})       # room again after delete
        await F.wait_done(again)
    finally:
        await m.aclose()


def test_disk_quota_while_streaming(tmp_path):
    with TestClient(_router_app(tmp_path, doc_disk_quota_mb=1)) as client:
        body, ctype = _multipart([("file", "a.txt", b"a" * (1024 * 1024 + 100))])
        r = client.post("/api/documents", content=_chunked(body), headers={"content-type": ctype})
        assert r.status_code == 507 and r.json()["detail"] == MSG_DISK_FULL
        assert not any((tmp_path / "data" / "jobs" / ".incoming").glob("*"))


@pytest.mark.anyio
async def test_failed_and_canceled_jobs_drop_their_input(tmp_path):
    tr = F.FakeTranslator()
    m = JobManager(settings=F.settings(tmp_path, doc_job_concurrency=1), translator=tr)
    await m.start()
    try:
        job, _ = await F.run_job(m, "x.docx", os.urandom(4096))
        assert job.status == "error"
        d = m.job_dir(job.id)
        assert not list(d.glob("input*")) and (d / "meta.json").is_file()
        tr.gate = asyncio.Event()
        first = await m.submit(F.make_minimal_docx(["Hello world"]), "a.docx", {})
        queued = await m.submit(F.make_minimal_docx(["Hello world"]), "b.docx", {})
        await m.cancel(queued.id)
        assert queued.status == "canceled" and not list(m.job_dir(queued.id).glob("input*"))
        await m.cancel(first.id)
        await F.wait_done(first)
        assert first.status == "canceled" and not list(m.job_dir(first.id).glob("input*"))
    finally:
        await m.aclose()


# ====================================================================== private files / retention (C7)
@pytest.mark.anyio
@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
async def test_job_files_are_private(tmp_path):
    old = os.umask(0o022)
    try:
        m = JobManager(settings=F.settings(tmp_path), translator=F.FakeTranslator())
        await m.start()
        try:
            job, _ = await F.run_job(m, "a.docx", F.make_minimal_docx(["Hello world"]))
            assert job.status == "done"
            d = m.job_dir(job.id)
            assert (m.root.stat().st_mode & 0o777) == 0o700
            assert (d.stat().st_mode & 0o777) == 0o700
            files = [p for p in d.iterdir() if p.is_file()]
            assert {p.name for p in files} >= {"input.docx", "output.docx", "meta.json", "preview.txt"}
            assert all((p.stat().st_mode & 0o777) == 0o600 for p in files), [(p.name, oct(p.stat().st_mode)) for p in files]
            part = m.new_upload_path()
            assert (part.stat().st_mode & 0o777) == 0o600
        finally:
            await m.aclose()
    finally:
        os.umask(old)


@pytest.mark.anyio
async def test_shorter_retention_applies_at_start(tmp_path):
    m = JobManager(settings=F.settings(tmp_path, doc_retention_hours=24), translator=F.FakeTranslator())
    await m.start()
    job, _ = await F.run_job(m, "a.docx", F.make_minimal_docx(["Hello world"]))
    await m.aclose()
    meta_path = m.job_dir(job.id) / "meta.json"
    meta = json.loads(meta_path.read_text("utf-8"))
    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    meta["finished_at"] = two_hours_ago.isoformat()
    meta_path.write_text(json.dumps(meta), "utf-8")
    m2 = JobManager(settings=F.settings(tmp_path, doc_retention_hours=1), translator=F.FakeTranslator())
    await m2.start()
    try:
        assert m2.get(job.id) is None and not m2.job_dir(job.id).exists()
    finally:
        await m2.aclose()


# ====================================================================== HWP budget
def _hwp_bomb(tmp_path, records: int):
    """The sample HWP with BodyText/Section0 replaced by a tiny deflate stream that
    expands to `records` PARA_TEXT records (same stream size: olefile can only overwrite)."""
    import olefile

    src = F.FIXTURES / "sample.hwp"
    if not src.is_file():
        pytest.skip("HWP fixture missing")
    path = tmp_path / "bomb.hwp"
    path.write_bytes(src.read_bytes())
    rec = struct.pack("<I", H.HWPTAG_PARA_TEXT | (0 << 10) | (2 << 20)) + "A".encode("utf-16-le")
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    bomb = c.compress(rec * records) + c.flush()
    with olefile.OleFileIO(str(path), write_mode=True) as ole:
        size = ole.get_size("BodyText/Section0")
        assert len(bomb) <= size, (len(bomb), size)
        ole.write_stream("BodyText/Section0", bomb + b"\0" * (size - len(bomb)))
    return path


def test_hwp_extraction_has_a_shared_budget(tmp_path, monkeypatch):
    path = _hwp_bomb(tmp_path, 90_000)
    monkeypatch.setattr(H, "MAX_PARAGRAPHS", 50_000)
    with pytest.raises(DocumentError) as e:
        H.hwp5_paragraphs(path)
    assert "너무 큰" in e.value.message
    monkeypatch.setattr(H, "MAX_PARAGRAPHS", 200_000)
    monkeypatch.setattr(H, "MAX_TOTAL_BYTES", 100_000)
    with pytest.raises(DocumentError):
        H.hwp5_paragraphs(path)
    monkeypatch.setattr(H, "MAX_TOTAL_BYTES", 128 * 1024 * 1024)
    assert len(H.hwp5_paragraphs(path)) == 90_000
    monkeypatch.setattr(H, "MAX_PARA_BYTES", 1)                  # one huge PARA_TEXT record
    with pytest.raises(DocumentError):
        H.hwp5_paragraphs(path)


def test_hwp_extraction_stops_at_max_chars():
    sample = F.FIXTURES / "sample.hwp"
    if not sample.is_file():
        pytest.skip("HWP fixture missing")
    assert H.hwp5_paragraphs(sample, max_chars=100_000)
    with pytest.raises(DocumentError) as e:
        H.hwp5_paragraphs(sample, max_chars=50)
    assert "너무 깁니다" in e.value.message


def test_hwp_fixture_has_no_personal_metadata():
    import olefile

    sample = F.FIXTURES / "sample.hwp"
    if not sample.is_file():
        pytest.skip("HWP fixture missing")
    with olefile.OleFileIO(str(sample)) as ole:
        props = ole.getproperties("\x05HwpSummaryInformation")
        author = str(props.get(4, "")).split("\x00", 1)[0]
        saved_by = str(props.get(8, "")).split("\x00", 1)[0]
        link = ole.openstream("DocOptions/_LinkDoc").read().decode("utf-16-le", "replace")
    assert author == "user" and saved_by == "user"
    assert "\\Users\\user\\" in link


# ====================================================================== zip / XML budget
def test_xml_part_limits(tmp_path, monkeypatch):
    many = b"<w:document xmlns:w='w'><w:body>" + b"<w:p/>" * 20_000 + b"</w:body></w:document>"
    assert ziputil.parse_xml(many).getroot() is not None
    monkeypatch.setattr(ziputil, "MAX_XML_ELEMENTS", 10_000)
    with pytest.raises(DocumentError) as e:
        ziputil.parse_xml(many)
    assert "너무 큰" in e.value.message
    # '<' in many end tags but few elements: exact count passes
    ok = b"<r>" + b"<a></a>" * 6_000 + b"</r>"
    assert ziputil.parse_xml(ok).getroot().tag == "r"
    monkeypatch.setattr(ziputil, "MAX_XML_ELEMENTS", 4_000_000)
    monkeypatch.setattr(ziputil, "MAX_XML_PART_BYTES", 50_000)
    with pytest.raises(DocumentError):
        ziputil.parse_xml(many)
    # rewrite_zip refuses a selected part by its declared size, before reading it
    path = tmp_path / "a.docx"
    path.write_bytes(F.make_minimal_docx(["Hello world"] * 2000))
    with zipfile.ZipFile(path) as z:
        size = z.getinfo("word/document.xml").file_size
    monkeypatch.setattr(ziputil, "MAX_XML_PART_BYTES", size - 1)
    with pytest.raises(DocumentError):
        ziputil.rewrite_zip(path, None, lambda n: n == "word/document.xml", lambda n, t: None)
    assert ziputil.MAX_TOTAL_UNCOMPRESSED <= 150 * 1024 * 1024


def test_rewrite_zip_pass1_does_not_serialise(tmp_path, monkeypatch):
    path = tmp_path / "a.docx"
    path.write_bytes(F.make_minimal_docx(["Hello world"] * 20))
    dumped: list[int] = []
    real_dump = ziputil.dump_xml

    def counting_dump(tree):
        dumped.append(1)
        return real_dump(tree)

    monkeypatch.setattr(ziputil, "dump_xml", counting_dump)
    seen: list[str] = []
    assert ziputil.rewrite_zip(path, None, lambda n: n == "word/document.xml",
                               lambda n, t: seen.append(n)) == ["word/document.xml"]
    assert seen == ["word/document.xml"] and dumped == []        # pass 1: transform only
    out = tmp_path / "b.docx"
    ziputil.rewrite_zip(path, out, lambda n: n == "word/document.xml", lambda n, t: None)
    assert dumped == [1] and zipfile.ZipFile(out).read("word/document.xml")


# ====================================================================== image EXIF
def _rotated_jpeg(w: int, h: int, orientation: int = 6) -> bytes:
    from PIL import Image

    im = Image.new("RGB", (w, h), (255, 255, 255))
    im.paste((255, 0, 0), (0, 0, w // 5, h // 5))                 # red block top-left of the raw frame
    exif = Image.Exif()
    exif[0x0112] = orientation
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif.tobytes(), quality=95)
    return buf.getvalue()


@pytest.mark.parametrize("size", [(4032, 3024), (1600, 1200)])
def test_image_embed_respects_exif_orientation(size):
    from PIL import Image

    from translator_app.documents.image import inspect_image

    info = inspect_image(_rotated_jpeg(*size))
    assert info.width < info.height                                 # portrait, as the camera shows it
    assert max(info.width, info.height) <= 2000
    with Image.open(io.BytesIO(info.embed)) as im:
        assert im.size == (info.width, info.height)
        assert (im.getexif().get(0x0112) or 1) == 1
        # orientation 6 = rotate 90° clockwise to display: the raw top-left block ends top-right
        r, g, b = im.convert("RGB").getpixel((im.size[0] - 5, 5))
        assert r > 200 and g < 80 and b < 80
