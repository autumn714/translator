"""Optional cookie login (UI_AUTH=1)."""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from conftest import make_settings
from translator_app.auth import COOKIE_NAME, LoginThrottle, SessionSigner, is_exempt
from translator_app.main import create_app


@pytest.fixture
def auth_client(tmp_path):
    settings = make_settings(tmp_path, ui_auth="1", ui_user="keei", ui_password="s3cret-pass")
    app = create_app(settings)
    app.state.login_throttle = LoginThrottle(delay=0.01)  # keep wrong-password tests fast
    with TestClient(app, follow_redirects=False) as client:
        yield client


def login(client: TestClient, username: str = "keei", password: str = "s3cret-pass"):
    return client.post("/api/login", json={"username": username, "password": password})


def test_unauthenticated_requests_are_blocked(auth_client) -> None:
    api = auth_client.get("/api/status")
    assert api.status_code == 401 and api.json() == {"detail": "로그인이 필요합니다."}
    assert auth_client.post("/api/translate", json={"text": "hi"}).status_code == 401
    page = auth_client.get("/")
    assert page.status_code == 302 and page.headers["location"] == "/login"
    assert auth_client.get("/static/app.js").status_code == 302
    for path in ("/health", "/login", "/static/styles.css"):
        assert auth_client.get(path).status_code == 200, path


def test_login_sets_a_signed_cookie_and_unlocks_the_api(auth_client) -> None:
    assert login(auth_client, password="wrong").status_code == 401
    response = login(auth_client)
    assert response.status_code == 200 and response.json()["user"] == "keei"
    header = response.headers["set-cookie"]
    assert header.startswith(f"{COOKIE_NAME}=")
    lowered = header.lower()
    assert "httponly" in lowered and "samesite=lax" in lowered and "secure" not in lowered
    assert "max-age=43200" in lowered
    status = auth_client.get("/api/status").json()
    assert status["auth"] == {"enabled": True, "user": "keei"}
    assert auth_client.get("/").status_code == 200
    assert auth_client.get("/login").status_code == 302  # already logged in
    assert auth_client.post("/api/logout").status_code == 200
    auth_client.cookies.clear()
    assert auth_client.get("/api/status").status_code == 401


def test_tampered_or_expired_cookie_is_rejected(auth_client) -> None:
    signer: SessionSigner = auth_client.app.state.session_signer
    token = signer.issue("keei")
    user, issued, session_id, signature = token.split("|")
    auth_client.cookies.set(COOKIE_NAME, f"{user}|{int(issued) + 1}|{session_id}|{signature}")
    assert auth_client.get("/api/status").status_code == 401
    auth_client.cookies.set(COOKIE_NAME, signer.issue("keei", now=time.time() - 13 * 3600))
    assert auth_client.get("/api/status").status_code == 401
    auth_client.cookies.set(COOKIE_NAME, signer.issue("someone-else"))
    assert auth_client.get("/api/status").status_code == 401
    other = SessionSigner(b"another-key-another-key")
    auth_client.cookies.set(COOKIE_NAME, other.issue("keei"))
    assert auth_client.get("/api/status").status_code == 401
    auth_client.cookies.set(COOKIE_NAME, signer.issue("keei"))
    assert auth_client.get("/api/status").status_code == 200


def test_wrong_passwords_never_lock_out_the_right_one(auth_client) -> None:
    # every browser arrives from the gate's address: failures must not lock everyone out
    for _ in range(12):
        assert login(auth_client, password="nope").status_code == 401
    assert login(auth_client).status_code == 200


def test_login_throttle_serializes_failures() -> None:
    async def scenario() -> float:
        throttle = LoginThrottle(delay=0.05)
        started = time.perf_counter()
        await asyncio.gather(*(throttle.failed() for _ in range(4)))
        return time.perf_counter() - started

    assert asyncio.run(scenario()) >= 0.19  # one failure at a time


def test_login_without_auth_is_a_no_op(mock_client) -> None:
    assert mock_client.post("/api/login", json={"username": "x", "password": "y"}).json() == {"ok": True, "user": None}
    assert mock_client.get("/login", follow_redirects=False).status_code == 302


def test_exempt_paths() -> None:
    assert is_exempt("/static/login.js") and is_exempt("/api/login") and not is_exempt("/api/glossaries")
