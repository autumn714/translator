"""Text translation API: /api/translate, /api/translate/stream, /api/alternatives, /api/rewrite, /api/lookup."""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from functools import partial
from typing import Any

import anyio
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from translator_app.languages import is_known_language
from translator_app.llm.client import LLMError, LLMUnavailable, TranslationCancelled, VisionUnavailable
from translator_app.schemas import (
    AlternativesRequest,
    AlternativesResponse,
    LookupRequest,
    LookupResponse,
    RewriteRequest,
    RewriteResponse,
    TranslateOptions,
    TranslationRequest,
    TranslationResponse,
)
from translator_app.services.glossary import normalize_language_code
from translator_app.services.translator import GlossaryNotFound, TranslatorService

logger = logging.getLogger("translator.api")
router = APIRouter(tags=["translate"])


def get_translator(request: Request) -> TranslatorService:
    return request.app.state.translator


def http_error(exc: Exception) -> HTTPException:
    """Map service exceptions to HTTP errors with Korean details."""
    if isinstance(exc, GlossaryNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, VisionUnavailable):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, LLMUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, LLMError):
        return HTTPException(status_code=502, detail=str(exc))
    if isinstance(exc, TranslationCancelled):
        return HTTPException(status_code=409, detail=str(exc))
    logger.exception("요청 처리 중 오류", exc_info=exc)
    return HTTPException(status_code=500, detail="서버 오류가 발생했습니다.")


def _known(code: str) -> bool:
    return is_known_language(code) or is_known_language(normalize_language_code(code))


def check_languages(source_lang: str, target_lang: str) -> None:
    if not _known(target_lang):
        raise HTTPException(status_code=400, detail=f"지원하지 않는 번역 언어입니다: {target_lang}")
    if source_lang and source_lang != "auto" and not _known(source_lang):
        raise HTTPException(status_code=400, detail=f"지원하지 않는 원문 언어입니다: {source_lang}")


def _normalized(opts: TranslateOptions) -> TranslateOptions:
    update: dict[str, Any] = {"target_lang": normalize_language_code(opts.target_lang)}
    if opts.source_lang and opts.source_lang != "auto":
        update["source_lang"] = normalize_language_code(opts.source_lang)
    return opts.model_copy(update=update)


def _check_text(request: Request, text: str) -> None:
    limit = request.app.state.settings.text_max_chars
    if len(text) > limit:
        raise HTTPException(status_code=413, detail=f"텍스트가 너무 깁니다 (최대 {limit:,}자).")


class NDJSONResponse(StreamingResponse):
    """Streams NDJSON and stops the generator as soon as the client disconnects
    (also while nothing is being sent yet), so upstream LLM requests are aborted."""

    media_type = "application/x-ndjson"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            async with anyio.create_task_group() as group:

                async def run_then_stop(func: Any) -> None:
                    await func()
                    group.cancel_scope.cancel()

                group.start_soon(run_then_stop, partial(self.stream_response, send))
                await run_then_stop(partial(self.listen_for_disconnect, receive))
        except OSError:
            pass  # client went away while sending
        except BaseExceptionGroup as group:
            if not all(isinstance(exc, OSError) for exc in group.exceptions):
                raise
        finally:
            closer = getattr(self.body_iterator, "aclose", None)
            if closer is not None:
                with anyio.CancelScope(shield=True):
                    await closer()


@router.post("/api/translate", response_model=TranslationResponse)
async def translate(payload: TranslationRequest, request: Request) -> TranslationResponse:
    _check_text(request, payload.text)
    check_languages(payload.source_lang, payload.target_lang)
    try:
        return await get_translator(request).translate_text(payload.text, _normalized(payload))
    except Exception as exc:  # noqa: BLE001
        raise http_error(exc) from exc


@router.post("/api/translate/stream")
async def translate_stream(payload: TranslationRequest, request: Request) -> StreamingResponse:
    _check_text(request, payload.text)
    check_languages(payload.source_lang, payload.target_lang)
    service = get_translator(request)
    opts = _normalized(payload)
    if opts.use_glossary and opts.glossary_id:
        try:
            await service.resolve_glossary(opts, "")
        except GlossaryNotFound as exc:
            raise http_error(exc) from exc

    async def lines() -> AsyncIterator[bytes]:
        async with aclosing(service.stream_text(payload.text, opts)) as events:
            async for event in events:
                yield (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")

    return NDJSONResponse(lines(), headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/api/alternatives", response_model=AlternativesResponse)
async def alternatives(payload: AlternativesRequest, request: Request) -> AlternativesResponse:
    check_languages(payload.source_lang, payload.target_lang)
    try:
        items = await get_translator(request).alternatives(_normalized(payload))  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        raise http_error(exc) from exc
    return AlternativesResponse(alternatives=items)


@router.post("/api/rewrite", response_model=RewriteResponse)
async def rewrite(payload: RewriteRequest, request: Request) -> RewriteResponse:
    if payload.lang and payload.lang != "auto" and not _known(payload.lang):
        raise HTTPException(status_code=400, detail=f"지원하지 않는 언어입니다: {payload.lang}")
    try:
        return await get_translator(request).rewrite(payload)
    except Exception as exc:  # noqa: BLE001
        raise http_error(exc) from exc


@router.post("/api/lookup", response_model=LookupResponse)
async def lookup(payload: LookupRequest, request: Request) -> LookupResponse:
    if not payload.term.strip():
        raise HTTPException(status_code=400, detail="찾을 단어를 입력하세요.")
    check_languages(payload.source_lang, payload.target_lang)
    try:
        return await get_translator(request).lookup(payload)
    except Exception as exc:  # noqa: BLE001
        raise http_error(exc) from exc
