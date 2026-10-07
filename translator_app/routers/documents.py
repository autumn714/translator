"""Document translation API (SPEC §7).

Uploads are parsed from the request stream and written straight to the job
directory in chunks, so a file larger than DOC_MAX_MB is rejected (413) as soon
as the limit is crossed, without spooling the whole body anywhere first.  The
whole body is bounded too (chunked requests carry no Content-Length): at most a
few parts, only the "options" field is kept, and DOC_DISK_QUOTA_MB is checked
while the file streams in (507).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from translator_app.documents import IMAGE_EXTS
from translator_app.documents.base import MSG_VISION_UNAVAILABLE, DocumentError
from translator_app.documents.jobs import MSG_DISK_FULL, Job, JobManager, sanitize_filename
from translator_app.documents.schemas import (
    DocumentJob,
    DocumentJobList,
    DocumentOptions,
    PreviewResponse,
    ReportResponse,
)
from translator_app.routers.system import vision_available

logger = logging.getLogger("translator.documents")

router = APIRouter(prefix="/api/documents", tags=["documents"])

MAX_FIELD_BYTES = 64 * 1024            # the "options" JSON
MAX_HEADER_BYTES = 8 * 1024            # headers of one multipart part
MAX_PARTS = 8                          # "file" + "options" (+ a few the browser may add)
BODY_OVERHEAD = 256 * 1024             # boundaries and part headers on top of file + options
MAX_LIST_IDS = 100

MSG_UNAVAILABLE = "문서 번역 기능을 사용할 수 없습니다."
MSG_NOT_FOUND = "작업을 찾을 수 없습니다. 보관 기간이 지나 삭제되었을 수 있습니다."
MSG_NOT_DONE = "번역이 아직 끝나지 않았습니다."
MSG_NO_FILE = "파일을 첨부해 주세요."
MSG_BAD_FORM = "업로드 형식이 올바르지 않습니다."
MSG_BAD_OPTIONS = "번역 옵션이 올바르지 않습니다."
MSG_OUTPUT_GONE = "번역된 파일을 찾을 수 없습니다."
MSG_IMAGE_NO_VISION = f"{MSG_VISION_UNAVAILABLE} 이미지 파일은 번역할 수 없습니다."

_MEDIA_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".hwpx": "application/hwp+zip",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".srt": "application/x-subrip; charset=utf-8",
    ".vtt": "text/vtt; charset=utf-8",
}


# ------------------------------------------------------------------ helpers
def _manager(request: Request) -> JobManager:
    manager = getattr(request.app.state, "job_manager", None)
    if manager is None or not getattr(manager, "ready", True):
        raise HTTPException(status_code=503, detail=MSG_UNAVAILABLE)
    return manager


def _job_or_404(manager: JobManager, job_id: str) -> Job:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=MSG_NOT_FOUND)
    return job


def _limit_message(max_mb: int) -> str:
    return f"파일이 너무 큽니다. 최대 {max_mb}MB까지 올릴 수 있습니다."


def content_disposition(filename: str) -> str:
    """attachment; filename="ascii fallback"; filename*=UTF-8''percent-encoded (RFC 6266/5987)."""
    name = sanitize_filename(filename)
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r'[^A-Za-z0-9._()\- ]+', "_", ascii_name).strip(" _") or "download"
    stem, dot, ext = name.rpartition(".")
    if dot and not ascii_name.lower().endswith("." + ext.lower()) and ext.isascii():
        ascii_name = f"{Path(ascii_name).stem or 'download'}.{ext}"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


def _decode_header_value(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("cp949")
        except UnicodeDecodeError:
            return raw.decode("latin-1")


class _UploadTooLarge(Exception):
    pass


class _DiskFull(Exception):
    pass


class _BadUpload(Exception):
    pass


class _RejectedType(Exception):
    pass


async def _receive_upload(request: Request, manager: JobManager,
                          rejected: frozenset[str] = frozenset()) -> tuple[Path, str, dict[str, str], int]:
    """Stream the multipart body: the part named "file" goes to disk, small fields to memory.
    A file whose extension is in ``rejected`` stops the upload as soon as its name arrives.
    -> (temp path, client filename, fields, file size)."""
    from python_multipart.exceptions import FormParserError
    from python_multipart.multipart import MultipartParser, parse_options_header

    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if ctype != b"multipart/form-data" or not boundary:
        raise _BadUpload(MSG_BAD_FORM)

    limit = manager.max_bytes
    body_limit = limit + MAX_FIELD_BYTES + BODY_OVERHEAD
    state: dict[str, Any] = {"hname": b"", "hvalue": b"", "disp": b"", "name": None, "is_file": False,
                             "buf": bytearray(), "done_file": False, "parts": 0, "hbytes": 0}
    fields: dict[str, str] = {}
    file_info: dict[str, Any] = {"filename": None, "size": 0, "pending": []}
    tmp = manager.new_upload_path()
    fh = None

    def on_part_begin() -> None:
        state["parts"] += 1
        if state["parts"] > MAX_PARTS:
            raise _BadUpload(MSG_BAD_FORM)
        state.update(disp=b"", name=None, is_file=False, buf=bytearray(), hname=b"", hvalue=b"", hbytes=0)

    def _header_bytes(n: int) -> None:
        state["hbytes"] += n
        if state["hbytes"] > MAX_HEADER_BYTES:
            raise _BadUpload(MSG_BAD_FORM)

    def on_header_field(data: bytes, start: int, end: int) -> None:
        _header_bytes(end - start)
        state["hname"] += data[start:end]

    def on_header_value(data: bytes, start: int, end: int) -> None:
        _header_bytes(end - start)
        state["hvalue"] += data[start:end]

    def on_header_end() -> None:
        if state["hname"].strip().lower() == b"content-disposition":
            state["disp"] = state["hvalue"]
        state["hname"], state["hvalue"] = b"", b""

    def on_headers_finished() -> None:
        _disp, opts = parse_options_header(state["disp"])
        state["name"] = _decode_header_value(opts.get(b"name", b""))
        if b"filename" in opts and state["name"] == "file" and not state["done_file"]:
            state["is_file"] = True
            file_info["filename"] = _decode_header_value(opts[b"filename"])
            if Path(sanitize_filename(file_info["filename"])).suffix.lower() in rejected:
                raise _RejectedType()

    def on_part_data(data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if state["is_file"]:
            file_info["size"] += len(chunk)
            if file_info["size"] > limit:
                raise _UploadTooLarge()
            file_info["pending"].append(chunk)
        elif state["name"] == "options":                # any other field is read past, not kept
            if len(state["buf"]) + len(chunk) > MAX_FIELD_BYTES:
                raise _BadUpload(MSG_BAD_OPTIONS)
            state["buf"] += chunk

    def on_part_end() -> None:
        if state["is_file"]:
            state["done_file"] = True
            state["is_file"] = False
        elif state["name"] == "options":
            fields["options"] = bytes(state["buf"]).decode("utf-8", "replace")

    parser = MultipartParser(boundary, {
        "on_part_begin": on_part_begin, "on_part_data": on_part_data, "on_part_end": on_part_end,
        "on_header_field": on_header_field, "on_header_value": on_header_value,
        "on_header_end": on_header_end, "on_headers_finished": on_headers_finished,
    })
    token = await asyncio.to_thread(manager.begin_upload)
    received = 0
    try:
        fh = await asyncio.to_thread(open, tmp, "wb")
        async for chunk in request.stream():
            received += len(chunk)
            if received > body_limit:
                raise _UploadTooLarge()
            parser.write(chunk)
            if file_info["pending"]:
                data = b"".join(file_info["pending"])
                file_info["pending"].clear()
                if not manager.track_upload(token, file_info["size"]):
                    raise _DiskFull()
                await asyncio.to_thread(fh.write, data)
        parser.finalize()
        await asyncio.to_thread(fh.close)
        fh = None
    except BaseException as exc:
        if fh is not None:
            fh.close()
        with contextlib.suppress(OSError):
            tmp.unlink()
        if isinstance(exc, FormParserError):
            raise _BadUpload(MSG_BAD_FORM) from exc
        raise
    finally:
        manager.end_upload(token)
    if not state["done_file"] or (not file_info["filename"] and file_info["size"] == 0):
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise _BadUpload(MSG_NO_FILE)
    return tmp, file_info["filename"] or "", fields, file_info["size"]


# ------------------------------------------------------------------ routes
@router.post("", status_code=202, response_model=DocumentJob)
async def upload_document(request: Request) -> Any:
    manager = _manager(request)
    max_mb = int(manager.settings.doc_max_mb)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > manager.max_bytes + 1024 * 1024:
        return JSONResponse(status_code=413, content={"detail": _limit_message(max_mb)},
                            headers={"Connection": "close"})
    # without vision the model cannot read images (same rule as document_formats in /api/status)
    rejected = frozenset() if vision_available(request) else IMAGE_EXTS
    try:
        tmp, filename, fields, _size = await _receive_upload(request, manager, rejected)
    except _RejectedType:
        return JSONResponse(status_code=415, content={"detail": MSG_IMAGE_NO_VISION}, headers={"Connection": "close"})
    except _UploadTooLarge:
        return JSONResponse(status_code=413, content={"detail": _limit_message(max_mb)},
                            headers={"Connection": "close"})
    except _DiskFull:
        return JSONResponse(status_code=507, content={"detail": MSG_DISK_FULL}, headers={"Connection": "close"})
    except _BadUpload as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        raw = fields.get("options") or "{}"
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
            options = DocumentOptions.model_validate(data)
        except (ValueError, ValidationError) as exc:
            raise HTTPException(status_code=422, detail=MSG_BAD_OPTIONS) from exc
        try:
            job = await manager.submit(tmp, filename, options)
        except DocumentError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        tmp = None
        return job.public()
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink()


@router.get("", response_model=DocumentJobList)
async def list_documents(request: Request, ids: str = "") -> DocumentJobList:
    manager = _manager(request)
    wanted = [i for i in (s.strip() for s in ids.split(",")) if i][:MAX_LIST_IDS]
    return DocumentJobList(jobs=[j.public() for j in manager.list(wanted)])


@router.get("/{job_id}", response_model=DocumentJob)
async def get_document(request: Request, job_id: str) -> DocumentJob:
    return _job_or_404(_manager(request), job_id).public()


@router.get("/{job_id}/download")
async def download_document(request: Request, job_id: str) -> FileResponse:
    manager = _manager(request)
    job = _job_or_404(manager, job_id)
    if job.status != "done":
        raise HTTPException(status_code=409, detail=MSG_NOT_DONE)
    path = manager.output_path(job)
    if path is None:
        raise HTTPException(status_code=404, detail=MSG_OUTPUT_GONE)
    name = job.output_filename or f"translated{path.suffix}"
    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        headers={"Content-Disposition": content_disposition(name), "Cache-Control": "no-store",
                 "X-Content-Type-Options": "nosniff"},
    )


@router.get("/{job_id}/preview", response_model=PreviewResponse)
async def preview_document(request: Request, job_id: str) -> PreviewResponse:
    manager = _manager(request)
    job = _job_or_404(manager, job_id)
    if job.status != "done":
        raise HTTPException(status_code=409, detail=MSG_NOT_DONE)
    text = await asyncio.to_thread(manager.preview_text, job)
    return PreviewResponse(text=text[:20_000])


@router.get("/{job_id}/report", response_model=ReportResponse)
async def report_document(request: Request, job_id: str) -> ReportResponse:
    manager = _manager(request)
    job = _job_or_404(manager, job_id)
    if job.status != "done":
        raise HTTPException(status_code=404, detail=MSG_NOT_DONE)
    items = await asyncio.to_thread(manager.report_items, job)
    return ReportResponse.model_validate({"items": items})


@router.post("/{job_id}/cancel", response_model=DocumentJob)
async def cancel_document(request: Request, job_id: str) -> DocumentJob:
    manager = _manager(request)
    job = await manager.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=MSG_NOT_FOUND)
    return job.public()


@router.delete("/{job_id}")
async def delete_document(request: Request, job_id: str) -> dict[str, bool]:
    manager = _manager(request)
    if not await manager.delete(job_id):
        raise HTTPException(status_code=404, detail=MSG_NOT_FOUND)
    return {"deleted": True}
