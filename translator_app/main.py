"""FastAPI application: wiring of settings, LLM client, translator service, routers and auth."""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from translator_app.auth import AuthMiddleware, LoginRateLimiter, SessionSigner
from translator_app.config import Settings, get_settings
from translator_app.engines.factory import build_engine
from translator_app.languages import LANGUAGES
from translator_app.llm.client import LLMClient
from translator_app.routers.glossaries import router as glossaries_router
from translator_app.routers.system import APP_VERSION
from translator_app.routers.system import router as system_router
from translator_app.routers.translate import router as translate_router
from translator_app.services.glossary import GlossaryStore
from translator_app.services.translator import TranslatorService

logger = logging.getLogger("translator")
STATIC_DIR = Path(__file__).parent / "static"


def _setup_logging() -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


class BodySizeLimit:
    """Reject requests whose Content-Length is obviously too large (413) before reading them."""

    def __init__(self, app: ASGIApp, *, document_bytes: int, import_bytes: int, default_bytes: int) -> None:
        self.app = app
        self.document_bytes = document_bytes
        self.import_bytes = import_bytes
        self.default_bytes = default_bytes

    def _limit(self, path: str) -> int:
        if path.startswith("/api/documents"):
            return self.document_bytes
        if path.startswith("/api/glossaries/") and path.endswith("/import"):
            return self.import_bytes
        return self.default_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            for name, value in scope.get("headers") or []:
                if name == b"content-length":
                    try:
                        length = int(value)
                    except ValueError:
                        length = 0
                    if length > self._limit(scope.get("path", "")):
                        body = json.dumps({"detail": "요청이 너무 큽니다."}, ensure_ascii=False).encode("utf-8")
                        await send({"type": "http.response.start", "status": 413, "headers": [
                            (b"content-type", b"application/json; charset=utf-8"),
                            (b"content-length", str(len(body)).encode("ascii")),
                            (b"connection", b"close"),
                        ]})
                        await send({"type": "http.response.body", "body": body})
                        return
                    break
        await self.app(scope, receive, send)


def _load_documents(settings: Settings, translator: TranslatorService) -> tuple[Any, Any]:
    """Optional subsystem: text translation must work even if PyMuPDF / lxml are missing."""
    try:
        from translator_app.documents.jobs import JobManager
        from translator_app.routers.documents import router as documents_router
    except ImportError as exc:
        logger.error("문서 번역 기능을 불러오지 못했습니다 (%s). 텍스트 번역만 제공합니다.", exc)
        return None, None
    try:
        job_manager = JobManager(settings=settings, translator=translator)
    except Exception:  # noqa: BLE001 - keep text translation available
        logger.exception("문서 번역 작업 관리자를 시작하지 못했습니다. 텍스트 번역만 제공합니다.")
        return None, None
    return job_manager, documents_router


def create_app(settings: Settings | None = None) -> FastAPI:
    _setup_logging()
    settings = settings or get_settings()
    glossary_store = GlossaryStore(settings.glossary_file)
    llm = None if settings.engine_type == "mock" else LLMClient(settings)
    engine = build_engine(settings, llm)
    translator = TranslatorService(settings, llm, engine, glossary_store)
    job_manager, documents_router = _load_documents(settings, translator)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if job_manager is not None:
            await job_manager.start()
        logger.info(
            "번역기 %s 시작 (엔진 %s, 모델 서버 %s, 문서 번역 %s, 로그인 %s)",
            APP_VERSION,
            engine.name,
            settings.llm_base_url if llm is not None else "-",
            "켜짐" if job_manager is not None else "꺼짐",
            "켜짐" if settings.ui_auth else "꺼짐",
        )
        try:
            yield
        finally:
            if job_manager is not None:
                try:
                    await job_manager.aclose()
                except Exception:  # noqa: BLE001
                    logger.exception("문서 작업 관리자 종료 중 오류")
            await engine.aclose()
            if llm is not None:
                await llm.aclose()

    app = FastAPI(title="번역기", version=APP_VERSION, lifespan=lifespan)
    app.state.settings = settings
    app.state.glossary_store = glossary_store
    app.state.llm = llm
    app.state.engine = engine
    app.state.translator = translator
    app.state.job_manager = job_manager
    app.state.session_signer = SessionSigner(settings.session_key)
    app.state.login_limiter = LoginRateLimiter()

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = []
        for error in exc.errors()[:3]:
            location = [str(part) for part in error.get("loc", ()) if part not in ("body", "query", "path", "form")]
            if location:
                fields.append(".".join(location))
        detail = "입력값이 올바르지 않습니다" + (f": {', '.join(fields)}" if fields else ".")
        return JSONResponse(status_code=422, content={"detail": detail})

    app.include_router(system_router)
    app.include_router(translate_router)
    app.include_router(glossaries_router)
    if documents_router is not None:
        app.include_router(documents_router)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    serialized_languages = json.dumps(LANGUAGES, ensure_ascii=False).replace("<", "\\u003c")
    index_path = STATIC_DIR / "index.html"

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> HTMLResponse:
        template = index_path.read_text(encoding="utf-8")
        return HTMLResponse(
            template.replace("__TRANSLATOR_LANGUAGES__", serialized_languages),
            headers={"Cache-Control": "no-cache"},
        )

    # middleware: the last added runs first → size check, CORS, then the login gate
    if settings.ui_auth:
        if not settings.ui_password:
            logger.error("UI_AUTH=1 이지만 UI_PASSWORD 가 비어 있어 아무도 로그인할 수 없습니다.")
        app.add_middleware(AuthMiddleware, signer=app.state.session_signer, user=settings.ui_user)
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_methods=["*"],
            allow_headers=["*"],
            allow_credentials=True,
        )
    app.add_middleware(
        BodySizeLimit,
        document_bytes=(settings.doc_max_mb + 2) * 1024 * 1024,
        import_bytes=6 * 1024 * 1024,
        default_bytes=4 * 1024 * 1024,
    )
    return app


app = create_app()
