"""Optional cookie login (UI_AUTH=1).

Cookie ``translator_session`` = ``<user b64>|<issued_at>|<session id>|<HMAC-SHA256 hex>``, HttpOnly,
SameSite=Lax, valid for 12 hours. No Secure flag: the app is served over plain HTTP inside the LAN.
The HMAC key is SESSION_SECRET bound to a fingerprint of UI_USER/UI_PASSWORD, so changing the password
invalidates every cookie; logout revokes the cookie's session id on the server. Each app start also mixes a
fresh random value into the key, so cookies issued before the start (including logged-out ones, whose
revocation lived only in memory) stop working and everyone logs in again after a restart.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from translator_app.config import Settings

COOKIE_NAME = "translator_session"
SESSION_TTL_SECONDS = 12 * 3600
LOGIN_FAILURE_DELAY = 1.0
MAX_REVOKED_SESSIONS = 50_000

MSG_LOGIN_REQUIRED = "로그인이 필요합니다."
MSG_LOGIN_FAILED = "아이디 또는 비밀번호가 올바르지 않습니다."

EXEMPT_PATHS = frozenset({"/login", "/api/login", "/api/logout", "/health", "/static/styles.css"})
EXEMPT_PREFIXES = ("/static/login.",)


def is_exempt(path: str) -> bool:
    if path in EXEMPT_PATHS:
        return True
    for prefix in EXEMPT_PREFIXES:
        if path.startswith(prefix):
            # only a file directly named login.* — never a path that climbs out of it ("/static/login./../x")
            rest = path[len(prefix):]
            return bool(rest) and "/" not in rest and "\\" not in rest and ".." not in rest
    return False


def _b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _unb64(value: str) -> str:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")


def session_signing_key(settings: Settings) -> bytes:
    """SESSION_SECRET bound to the current credentials: a new UI_USER / UI_PASSWORD invalidates old cookies."""
    fingerprint = hashlib.sha256(f"{settings.ui_user}\0{settings.ui_password}".encode("utf-8")).digest()
    return hmac.new(settings.session_key, b"translator-session\0" + fingerprint, hashlib.sha256).digest()


class SessionSigner:
    def __init__(self, key: bytes, *, ttl: int = SESSION_TTL_SECONDS) -> None:
        # per-start salt: tokens from an earlier process never verify (logout survives restarts)
        self._key = hmac.new(key, b"boot\0" + secrets.token_bytes(16), hashlib.sha256).digest()
        self.ttl = ttl
        self._revoked: dict[str, float] = {}  # session id -> time after which the token expires anyway

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def issue(self, user: str, *, now: float | None = None) -> str:
        issued = int(now if now is not None else time.time())
        payload = f"{_b64(user)}|{issued}|{secrets.token_urlsafe(12)}"
        return f"{payload}|{self._sign(payload)}"

    def _parse(self, token: str | None, now: float | None) -> tuple[str, int, str] | None:
        """(user, issued_at, session id) if the token is authentic and not expired."""
        if not token or token.count("|") != 3:
            return None
        user_part, issued_part, session_id, signature = token.split("|")
        expected = self._sign(f"{user_part}|{issued_part}|{session_id}")
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
        return user, issued, session_id

    def verify(self, token: str | None, *, now: float | None = None) -> str | None:
        """User name if the token is authentic, not expired and not logged out."""
        parsed = self._parse(token, now)
        if parsed is None or parsed[2] in self._revoked:
            return None
        return parsed[0]

    def revoke(self, token: str | None, *, now: float | None = None) -> bool:
        """Log the session out on the server (only authentic tokens are remembered)."""
        parsed = self._parse(token, now)
        if parsed is None:
            return False
        current = time.time() if now is None else now
        expired = [sid for sid, until in self._revoked.items() if until < current]
        for sid in expired:
            del self._revoked[sid]
        while len(self._revoked) >= MAX_REVOKED_SESSIONS:
            del self._revoked[min(self._revoked, key=self._revoked.__getitem__)]
        self._revoked[parsed[2]] = parsed[1] + self.ttl + 60
        return True


def check_credentials(settings: Settings, username: str, password: str) -> bool:
    """Constant-time comparison of both fields (always compares both)."""
    user_ok = hmac.compare_digest(username.encode("utf-8"), settings.ui_user.encode("utf-8"))
    pass_ok = hmac.compare_digest(password.encode("utf-8"), settings.ui_password.encode("utf-8"))
    return user_ok and pass_ok and bool(settings.ui_password)


class LoginThrottle:
    """Global brake for password guesses. Every login attempt checks the password while holding one
    lock, and a wrong one keeps holding it for ``delay`` seconds, so all clients together get about one
    guess per second. A correct password is answered at once but waits behind wrong guesses already
    queued (accepted trade-off). There is no per-client lockout: every browser reaches the app through
    the gate's address, so a lockout would lock everyone out."""

    def __init__(self, delay: float = LOGIN_FAILURE_DELAY) -> None:
        self.delay = delay
        self._lock: asyncio.Lock | None = None

    async def attempt(self, check: Callable[[], bool]) -> bool:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if check():
                return True
            await asyncio.sleep(self.delay)
            return False


def session_token_from_scope(scope: Scope) -> str | None:
    for name, value in scope.get("headers") or []:
        if name != b"cookie":
            continue
        for part in value.decode("latin-1").split(";"):
            key, sep, raw = part.strip().partition("=")
            if sep and key == COOKIE_NAME:
                return raw.strip().strip('"')
    return None


def session_user_from_scope(scope: Scope, signer: SessionSigner) -> str | None:
    token = session_token_from_scope(scope)
    return signer.verify(token) if token is not None else None


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
