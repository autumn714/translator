"""Glossary CRUD + CSV/TSV import and export."""
from __future__ import annotations

import asyncio
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile

from translator_app.languages import is_known_language
from translator_app.schemas import (
    GlossaryCreateRequest,
    GlossaryDocument,
    GlossaryEntry,
    GlossaryListResponse,
    GlossaryResponse,
    GlossaryUpdateRequest,
)
from translator_app.services.glossary import (
    MAX_IMPORT_BYTES,
    GlossaryImportError,
    GlossaryStore,
    export_entries,
    merge_entries,
    normalize_language_code,
    parse_import,
)

router = APIRouter(tags=["glossaries"])

MSG_NOT_FOUND = "용어집을 찾을 수 없습니다."
MAX_ENTRIES = 20000


def _store(request: Request) -> GlossaryStore:
    return request.app.state.glossary_store


def _load(store: GlossaryStore, glossary_id: str) -> GlossaryDocument:
    try:
        document = store.load_glossary(glossary_id)
    except ValueError:
        document = None
    if document is None:
        raise HTTPException(status_code=404, detail=MSG_NOT_FOUND)
    return document


def _check_entries(entries: list[GlossaryEntry]) -> list[GlossaryEntry]:
    if len(entries) > MAX_ENTRIES:
        raise HTTPException(status_code=400, detail=f"용어가 너무 많습니다 (최대 {MAX_ENTRIES:,}개).")
    checked: list[GlossaryEntry] = []
    for entry in entries:
        source_lang = normalize_language_code(entry.source_lang)
        target_lang = normalize_language_code(entry.target_lang)
        for code in (source_lang, target_lang):
            if not is_known_language(code):
                raise HTTPException(status_code=400, detail=f"알 수 없는 언어 코드입니다: {code}")
        checked.append(entry.model_copy(update={
            "source_lang": source_lang,
            "target_lang": target_lang,
            "source": entry.source.strip(),
            "target": entry.target.strip(),
            "note": entry.note.strip(),
        }))
    for entry in checked:
        if not entry.source or not entry.target:
            raise HTTPException(status_code=400, detail="원문과 번역을 모두 입력하세요.")
    return checked


@router.get("/api/glossaries", response_model=GlossaryListResponse)
async def list_glossaries(request: Request) -> GlossaryListResponse:
    store = _store(request)
    summaries = await asyncio.to_thread(store.list_glossaries)
    return GlossaryListResponse(glossaries=summaries, default_glossary_id=summaries[0].id if summaries else None)


@router.get("/api/glossaries/{glossary_id}", response_model=GlossaryResponse)
async def get_glossary(glossary_id: str, request: Request) -> GlossaryResponse:
    return GlossaryResponse(glossary=await asyncio.to_thread(_load, _store(request), glossary_id))


@router.post("/api/glossaries", response_model=GlossaryResponse, status_code=201)
async def create_glossary(payload: GlossaryCreateRequest, request: Request) -> GlossaryResponse:
    entries = _check_entries(payload.entries)
    glossary = await asyncio.to_thread(_store(request).create_glossary, name=payload.name.strip() or "용어집",
                                       entries=entries)
    return GlossaryResponse(glossary=glossary)


@router.put("/api/glossaries/{glossary_id}", response_model=GlossaryResponse)
async def update_glossary(glossary_id: str, payload: GlossaryUpdateRequest, request: Request) -> GlossaryResponse:
    store = _store(request)
    await asyncio.to_thread(_load, store, glossary_id)
    entries = _check_entries(payload.entries)
    document = GlossaryDocument(id=glossary_id, name=payload.name.strip() or "용어집", entries=entries)
    return GlossaryResponse(glossary=await asyncio.to_thread(store.save_glossary, document))


@router.delete("/api/glossaries/{glossary_id}", status_code=204)
async def delete_glossary(glossary_id: str, request: Request) -> Response:
    try:
        deleted = await asyncio.to_thread(_store(request).delete_glossary, glossary_id)
    except ValueError:
        deleted = False
    if not deleted:
        raise HTTPException(status_code=404, detail=MSG_NOT_FOUND)
    return Response(status_code=204)


@router.get("/api/glossaries/{glossary_id}/export")
async def export_glossary(glossary_id: str, request: Request, format: Literal["csv", "tsv"] = "csv") -> Response:  # noqa: A002
    document = await asyncio.to_thread(_load, _store(request), glossary_id)
    content = export_entries(document.entries, format)
    filename = f"{document.name}.{format}"
    fallback = f"glossary.{format}"
    media_type = "text/csv; charset=utf-8" if format == "csv" else "text/tab-separated-values; charset=utf-8"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}",
            "Cache-Control": "no-store",
        },
    )


@router.post("/api/glossaries/{glossary_id}/import", response_model=GlossaryResponse)
async def import_glossary(
    glossary_id: str,
    request: Request,
    file: UploadFile = File(...),
    mode: Literal["append", "replace"] = Form("append"),
    source_lang: str = Form("en"),
    target_lang: str = Form("ko"),
) -> GlossaryResponse:
    store = _store(request)
    document = await asyncio.to_thread(_load, store, glossary_id)
    data = await file.read(MAX_IMPORT_BYTES + 1)
    source_default = normalize_language_code(source_lang or "en")
    target_default = normalize_language_code(target_lang or "ko")
    for code in (source_default, target_default):
        if not is_known_language(code):
            raise HTTPException(status_code=400, detail=f"알 수 없는 언어 코드입니다: {code}")
    try:
        imported = await asyncio.to_thread(
            parse_import,
            data,
            file.filename or "",
            default_source_lang=source_default,
            default_target_lang=target_default,
        )
    except GlossaryImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    imported = _check_entries(imported)
    entries = imported if mode == "replace" else merge_entries(document.entries, imported)
    entries = _check_entries(entries)
    saved = await asyncio.to_thread(store.save_glossary, document.model_copy(update={"entries": entries}))
    return GlossaryResponse(glossary=saved)
