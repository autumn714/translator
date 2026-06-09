from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from translator_app.config import get_settings
from translator_app.engines.factory import build_engine
from translator_app.languages import LANGUAGES
from translator_app.schemas import (
    DetectedLanguage,
    GlossaryCreateRequest,
    GlossaryDocument,
    GlossaryListResponse,
    GlossaryResponse,
    GlossaryUpdateRequest,
    TranslationRequest,
    TranslationResponse,
    TranslationSegment,
)
from translator_app.services.glossary import GlossaryStore, match_glossary_entries
from translator_app.services.language_detection import detect_source_languages, resolve_source_language
from translator_app.services.segmentation import normalize_line_breaks


def create_app() -> FastAPI:
    settings = get_settings()
    engine = build_engine(settings)
    glossary_store = GlossaryStore(settings.glossary_path)

    app = FastAPI(title="Translator App", version="0.1.0")
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    static_dir = Path(__file__).parent / "static"
    index_template = (static_dir / "index.html").read_text(encoding="utf-8")
    serialized_languages = json.dumps(LANGUAGES, ensure_ascii=False).replace("<", "\\u003c")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    app.state.settings = settings
    app.state.engine = engine
    app.state.glossary_store = glossary_store

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await engine.aclose()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "engine": engine.name}

    @app.get("/api/glossaries", response_model=GlossaryListResponse)
    async def list_glossaries(request: Request) -> GlossaryListResponse:
        store = request.app.state.glossary_store
        return GlossaryListResponse(
            glossaries=store.list_glossaries(),
            default_glossary_id=store.default_glossary_id(),
        )

    @app.get("/api/glossaries/{glossary_id}", response_model=GlossaryResponse)
    async def get_glossary(glossary_id: str, request: Request) -> GlossaryResponse:
        glossary = request.app.state.glossary_store.load_glossary(glossary_id)
        if glossary is None:
            raise HTTPException(status_code=404, detail="Glossary not found")
        return GlossaryResponse(glossary=glossary)

    @app.post("/api/glossaries", response_model=GlossaryResponse, status_code=201)
    async def create_glossary(
        payload: GlossaryCreateRequest,
        request: Request,
    ) -> GlossaryResponse:
        glossary = request.app.state.glossary_store.create_glossary(
            name=payload.name,
            entries=payload.entries,
        )
        return GlossaryResponse(glossary=glossary)

    @app.put("/api/glossaries/{glossary_id}", response_model=GlossaryResponse)
    async def update_glossary(
        glossary_id: str,
        payload: GlossaryUpdateRequest,
        request: Request,
    ) -> GlossaryResponse:
        store = request.app.state.glossary_store
        if store.load_glossary(glossary_id) is None:
            raise HTTPException(status_code=404, detail="Glossary not found")
        glossary = store.save_glossary(
            GlossaryDocument(
                id=glossary_id,
                name=payload.name,
                entries=payload.entries,
            )
        )
        return GlossaryResponse(glossary=glossary)

    @app.delete("/api/glossaries/{glossary_id}", status_code=204)
    async def delete_glossary(glossary_id: str, request: Request) -> Response:
        deleted = request.app.state.glossary_store.delete_glossary(glossary_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Glossary not found")
        return Response(status_code=204)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(
            index_template.replace("__TRANSLATOR_LANGUAGES__", serialized_languages)
        )

    @app.post("/api/translate", response_model=TranslationResponse)
    async def translate(
        payload: TranslationRequest,
        request: Request,
    ) -> TranslationResponse:
        started = time.perf_counter()
        translation_engine = request.app.state.engine
        source_segments = translation_engine.segment_source_text(payload.text)
        detection = detect_source_languages(payload.text)
        resolved_source_lang = resolve_source_language(payload.source_lang, detection)
        glossary_store = request.app.state.glossary_store
        glossary_hits = []
        glossary_applied = False
        glossary_name = None

        identity_shortcut = resolved_source_lang != "auto" and resolved_source_lang == payload.target_lang

        if payload.use_glossary and not identity_shortcut:
            glossary_entries = payload.glossary_entries
            if payload.glossary_id:
                glossary = glossary_store.load_glossary(payload.glossary_id)
                if glossary is None:
                    raise HTTPException(status_code=404, detail="Selected glossary was not found")
                glossary_name = glossary.name
                if glossary_entries is None:
                    glossary_entries = glossary.entries

            if glossary_entries is None:
                glossary_entries = []

            glossary_hits = match_glossary_entries(
                payload.text,
                glossary_entries,
                target_lang=payload.target_lang,
                source_lang=resolved_source_lang,
            )
            glossary_applied = bool(glossary_hits)

        if identity_shortcut:
            result = TranslationResponse(
                translation=normalize_line_breaks(payload.text),
                segments=[
                    TranslationSegment(source=segment, target=segment)
                    for segment in source_segments
                ],
                glossary_hits=[],
                glossary_applied=False,
                glossary_name=glossary_name,
                detected_source_languages=[
                    DetectedLanguage(
                        code=item.code,
                        char_count=item.char_count,
                        share=item.share,
                    )
                    for item in detection.languages
                ],
                primary_source_lang=detection.primary_language,
                source_language_mode=detection.mode,
                engine=translation_engine.name,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            return result

        try:
            result = await translation_engine.translate(
                payload.text,
                resolved_source_lang,
                payload.target_lang,
                glossary_hits,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        paired_segments = [
            TranslationSegment(source=source, target=target)
            for source, target in zip(source_segments, result.segments, strict=False)
        ]
        return TranslationResponse(
            translation=result.translation,
            segments=paired_segments,
            glossary_hits=glossary_hits,
            glossary_applied=glossary_applied,
            glossary_name=glossary_name,
            detected_source_languages=[
                DetectedLanguage(
                    code=item.code,
                    char_count=item.char_count,
                    share=item.share,
                )
                for item in detection.languages
            ],
            primary_source_lang=detection.primary_language,
            source_language_mode=detection.mode,
            engine=translation_engine.name,
            latency_ms=latency_ms,
        )

    return app


app = create_app()
