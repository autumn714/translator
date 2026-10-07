"""/health, /api/status, login page and session endpoints."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from translator_app.auth import (
    COOKIE_NAME,
    MSG_LOGIN_FAILED,
    SESSION_TTL_SECONDS,
    check_credentials,
    session_token_from_scope,
    session_user_from_scope,
)
from translator_app.schemas import AuthStatus, LimitsStatus, LoginRequest, ModelStatus, StatusResponse

logger = logging.getLogger("translator.system")
router = APIRouter(tags=["system"])

APP_VERSION = "2.0.0"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

_FALLBACK_LOGIN = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>로그인 · 번역기</title>
<style>body{font-family:system-ui,"Malgun Gothic",sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;
background:#f4f5f7}form{background:#fff;padding:28px;border-radius:12px;box-shadow:0 2px 12px #0002;display:grid;gap:12px;
width:min(320px,90vw)}input,button{font:inherit;padding:10px;border-radius:8px;border:1px solid #ccd}button{background:#1d5bd6;
color:#fff;border:0}p{color:#c33;margin:0;min-height:1.2em}</style></head>
<body><form id="f"><h1 style="margin:0;font-size:20px">번역기</h1>
<input id="u" autocomplete="username" placeholder="아이디" required>
<input id="p" type="password" autocomplete="current-password" placeholder="비밀번호" required>
<button>로그인</button><p id="e" role="alert"></p></form>
<script>document.getElementById("f").onsubmit=async(ev)=>{ev.preventDefault();const r=await fetch("/api/login",{method:"POST",
headers:{"Content-Type":"application/json"},body:JSON.stringify({username:document.getElementById("u").value,
password:document.getElementById("p").value})});if(r.ok){location.replace("/");return}let d="";try{d=(await r.json()).detail}
catch(e){}document.getElementById("e").textContent=d||"로그인하지 못했습니다.";};</script></body></html>"""


def vision_available(request: Request) -> bool:
    llm = getattr(request.app.state, "llm", None)
    return True if llm is None else bool(llm.vision_enabled)


def document_formats(request: Request) -> list[dict[str, Any]]:
    if getattr(request.app.state, "job_manager", None) is None:
        return []
    try:
        from translator_app.documents import supported_formats
    except ImportError:
        return []
    try:
        # without vision the model cannot read images: do not offer image formats
        return list(supported_formats(vision=vision_available(request)))
    except Exception:  # noqa: BLE001 - status must never fail
        logger.exception("문서 형식 목록을 읽지 못했습니다")
        return []


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/status", response_model=StatusResponse)
async def status(request: Request) -> StatusResponse:
    settings = request.app.state.settings
    translator = request.app.state.translator
    model = await translator.model_status()
    user = getattr(request.state, "user", None) if settings.ui_auth else None
    return StatusResponse(
        app_version=APP_VERSION,
        engine=translator.engine_name,
        model=ModelStatus(**model),
        auth=AuthStatus(enabled=settings.ui_auth, user=user),
        limits=LimitsStatus(
            text_max_chars=settings.text_max_chars,
            doc_max_mb=settings.doc_max_mb,
            doc_retention_hours=settings.doc_retention_hours,
        ),
        document_formats=document_formats(request),
    )


@router.get("/login", response_model=None)
async def login_page(request: Request) -> Response:
    settings = request.app.state.settings
    signer = request.app.state.session_signer
    if not settings.ui_auth or session_user_from_scope(request.scope, signer) == settings.ui_user:
        return RedirectResponse("/", status_code=302)
    page = STATIC_DIR / "login.html"
    if page.is_file():
        return FileResponse(page, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})
    return HTMLResponse(_FALLBACK_LOGIN, headers={"Cache-Control": "no-store"})


@router.post("/api/login")
async def login(payload: LoginRequest, request: Request) -> JSONResponse:
    settings = request.app.state.settings
    if not settings.ui_auth:
        return JSONResponse({"ok": True, "user": None})
    username, password = payload.username.strip(), payload.password
    # checked under the global lock; a wrong one holds it for a second: one guess per second for everyone
    if not await request.app.state.login_throttle.attempt(lambda: check_credentials(settings, username, password)):
        logger.warning("로그인 실패")
        raise HTTPException(status_code=401, detail=MSG_LOGIN_FAILED)
    response = JSONResponse({"ok": True, "user": settings.ui_user})
    response.set_cookie(
        COOKIE_NAME,
        request.app.state.session_signer.issue(settings.ui_user),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=False,  # plain HTTP inside the LAN
        path="/",
    )
    return response


@router.post("/api/logout")
async def logout(request: Request) -> JSONResponse:
    token = session_token_from_scope(request.scope)
    if token is not None:
        request.app.state.session_signer.revoke(token)  # a copied cookie stops working too
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax")
    return response
