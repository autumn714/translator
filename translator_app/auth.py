"""Optional cookie login (UI_AUTH=1).

Cookie ``translator_session`` = ``<user b64>|<issued_at>|<HMAC-SHA256 hex>``, HttpOnly, SameSite=Lax,
valid for 12 hours. No Secure flag: the app is served over plain HTTP inside the LAN.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections import deque
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from translator_app.config import Settings

COOKIE_NAME = "translator_session"
SESSION_TTL_SECONDS = 12 * 3600
LOGIN_FAILURE_LIMIT = 10
LOGIN_FAILURE_WINDOW = 300.0

MSG_LOGIN_REQUIRED = "로그인이 필요합니다."
MSG_LOGIN_FAILED = "아이디 또는 비밀번호가 올바르지 않습니다."
MSG_LOGIN_LOCKED = "로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요."

EXEMPT_PATHS = frozenset({"/login", "/api/login", "/api/logout", "/health", "/static/styles.css"})
EXEMPT_PREFIXES = ("/static/login.",)


def is_exempt(path: str) -> bool:
    return path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES)


def _b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _unb64(value: str) -> str:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")


class SessionSigner:
    def __init__(self, key: bytes, *, ttl: int = SESSION_TTL_SECONDS) -> None:
        self._key = key
        self.ttl = ttl

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def issue(self, user: str, *, now: float | None = None) -> str:
        payload = f"{_b64(user)}|{int(now if now is not None else time.time())}"
        return f"{payload}|{self._sign(payload)}"

    def verify(self, token: str | None, *, now: float | None = None) -> str | None:
        """User name if the token is authentic and not expired."""
        if not token or token.count("|") != 2:
            return None
        user_part, issued_part, signature = token.split("|")
        expected = self._sign(f"{user_part}|{issued_part}")
        if not hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii", "replace")):
            return None
        try:
            issued = int(issued_part)
            user = _unb64(user_part)
        except (ValueError, binascii.Error, UnicodeDecodeError):
            return None
        current = time.time() if now is None else now
        if issued > current + 60 or current - issued > self.ttl:
            return None
        return user


def check_credentials(settings: Settings, username: str, password: str) -> bool:
    """Constant-time comparison of both fields (always compares both)."""
    user_ok = hmac.compare_digest(username.encode("utf-8"), settings.ui_user.encode("utf-8"))
    pass_ok = hmac.compare_digest(password.encode("utf-8"), settings.ui_password.encode("utf-8"))
    return user_ok and pass_ok and bool(settings.ui_password)


class LoginRateLimiter:
    """In-memory: at most LOGIN_FAILURE_LIMIT failures per client within LOGIN_FAILURE_WINDOW seconds."""

    def __init__(self, limit: int = LOGIN_FAILURE_LIMIT, window: float = LOGIN_FAILURE_WINDOW) -> None:
        self.limit = limit
        self.window = window
        self._failures: dict[str, deque[float]] = {}

    def _prune(self, client: str, now: float) -> deque[float]:
        bucket = self._failures.setdefault(client, deque())
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        if len(self._failures) > 10000:  # bound memory
            for key in [k for k, v in self._failures.items() if not v]:
                del self._failures[key]
        return bucket

    def blocked(self, client: str, *, now: float | None = None) -> bool:
        return len(self._prune(client, time.monotonic() if now is None else now)) >= self.limit

    def failure(self, client: str, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        self._prune(client, current).append(current)

    def success(self, client: str) -> None:
        self._failures.pop(client, None)


def session_user_from_scope(scope: Scope, signer: SessionSigner) -> str | None:
    for name, value in scope.get("headers") or []:
        if name != b"cookie":
            continue
        for part in value.decode("latin-1").split(";"):
            key, sep, raw = part.strip().partition("=")
            if sep and key == COOKIE_NAME:
                return signer.verify(raw.strip().strip('"'))
    return None


class AuthMiddleware:
    """Pure ASGI gate: unauthenticated API calls → 401 JSON, pages → 302 /login."""

    def __init__(self, app: ASGIApp, *, signer: SessionSigner, user: str) -> None:
        self.app = app
        self.signer = signer
        self.user = user

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        user = session_user_from_scope(scope, self.signer)
        if user is not None and hmac.compare_digest(user.encode("utf-8"), self.user.encode("utf-8")):
            scope.setdefault("state", {})["user"] = user
            await self.app(scope, receive, send)
            return
        if is_exempt(path):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        if path.startswith("/api/") or scope.get("method", "GET") not in ("GET", "HEAD"):
            await _send(send, 401, json.dumps({"detail": MSG_LOGIN_REQUIRED}, ensure_ascii=False).encode("utf-8"),
                        [(b"content-type", b"application/json; charset=utf-8")])
            return
        await _send(send, 302, b"", [(b"location", b"/login"), (b"cache-control", b"no-store")])


async def _send(send: Send, status: int, body: bytes, headers: list[tuple[bytes, bytes]]) -> None:
    all_headers: list[Any] = [*headers, (b"content-length", str(len(body)).encode("ascii"))]
    await send({"type": "http.response.start", "status": status, "headers": all_headers})
    await send({"type": "http.response.body", "body": body})
