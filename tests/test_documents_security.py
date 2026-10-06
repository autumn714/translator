"""Hostile inputs: zip bombs, path traversal, XXE, oversized HWP sections."""
from __future__ import annotations

import io
import zipfile

import docfixtures as F
import pytest

from translator_app.documents import ziputil
from translator_app.documents.base import DocumentError
from translator_app.documents.jobs import JobManager
from translator_app.documents.ziputil import check_zip, parse_xml


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _write(tmp_path, name: str, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_zip_member_limits(tmp_path, monkeypatch):
    p = _write(tmp_path, "a.docx", F.make_minimal_docx(["Hello world"]))
    check_zip(p, ("word/document.xml",))
    monkeypatch.setattr(ziputil, "MAX_TOTAL_UNCOMPRESSED", 100)
    with pytest.raises(DocumentError) as e:
        check_zip(p, ("word/document.xml",))
    assert "너무 큰" in e.value.message
    monkeypatch.setattr(ziputil, "MAX_TOTAL_UNCOMPRESSED", 500 * 1024 * 1024)
    monkeypatch.setattr(ziputil, "MAX_MEMBERS", 2)
    with pytest.raises(DocumentError):
        check_zip(p, ("word/document.xml",))


def test_zip_bomb_is_refused(tmp_path):
    """A member declaring ~4 GB uncompressed is refused before anything is inflated."""
    import struct

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "<w:document/>")
        z.writestr("word/bomb.bin", b"\0" * 1000)
    data = bytearray(buf.getvalue())
    pos = 0
    while (pos := data.find(b"PK\x01\x02", pos)) >= 0:             # central directory entries
        name_len = struct.unpack_from("<H", data, pos + 28)[0]
        if data[pos + 46:pos + 46 + name_len] == b"word/bomb.bin":
            struct.pack_into("<I", data, pos + 24, 0xF0000000)
        pos += 4
    p = _write(tmp_path, "bomb.docx", bytes(data))
    with pytest.raises(DocumentError) as e:
        check_zip(p, ("word/document.xml",))
    assert "너무 큰" in e.value.message


@pytest.mark.parametrize("name", ["../evil.xml", "/abs.xml", "C:/win.xml", "a/../../b.xml"])
def test_path_traversal_names(tmp_path, name):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<x/>")
        z.writestr(name, "<x/>")
    p = _write(tmp_path, "t.docx", buf.getvalue())
    with pytest.raises(DocumentError) as e:
        check_zip(p, ("word/document.xml",))
    assert "안전하지 않은" in e.value.message


def test_xxe_and_dtd_refused():
    for doc in (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>',
                b'<?xml version="1.0"?><!DOCTYPE r SYSTEM "http://example.com/x.dtd"><r/>',
                b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><r>&b;</r>'):
        with pytest.raises(DocumentError):
            parse_xml(doc)
    with pytest.raises(DocumentError):
        parse_xml(b"<r><unclosed></r>")
    assert parse_xml(b"<r>ok</r>").getroot().text == "ok"


@pytest.mark.anyio
async def test_xxe_docx_job_fails_cleanly(tmp_path):
    evil = F.make_minimal_docx(["Hello"])
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(evil)) as zin, zipfile.ZipFile(buf, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "word/document.xml":
                data = data.replace(b"?>", b'?><!DOCTYPE w:document [<!ENTITY x SYSTEM "file:///etc/hosts">]>', 1)
                data = data.replace(b"Hello", b"&x;")
            zout.writestr(info, data)
    m = JobManager(settings=F.settings(tmp_path), translator=F.FakeTranslator())
    await m.start()
    try:
        job, out = await F.run_job(m, "evil.docx", buf.getvalue())
        assert job.status == "error" and "DTD" in job.error and out is None
        job, out = await F.run_job(m, "traversal.hwpx", _traversal_hwpx())
        assert job.status == "error" and "안전하지 않은" in job.error
    finally:
        await m.aclose()


def _traversal_hwpx() -> bytes:
    src = F.make_hwpx()
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(src)) as zin, zipfile.ZipFile(buf, "w") as zout:
        for info in zin.infolist():
            zout.writestr(info, zin.read(info.filename))
        zout.writestr("../../outside.txt", b"x")
    return buf.getvalue()


def test_html_parser_does_not_fetch_entities():
    from translator_app.documents.html import translate_html

    data = (b'<!DOCTYPE html [<!ENTITY x SYSTEM "file:///etc/passwd">]><html><body><p>Hello &x; world</p>'
            b"</body></html>")
    out = translate_html(data, lambda xs: ["[KO] " + x for x in xs]).decode("utf-8")
    assert "root:" not in out and "[KO]" in out
