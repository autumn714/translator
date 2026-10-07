"""Optional cookie login (UI_AUTH=1)."""
from __future__ import annotations

import asyncio
import time

import httpx
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
        results = await asyncio.gather(*(throttle.attempt(lambda: False) for _ in range(4)))
        assert results == [False] * 4
        return time.perf_counter() - started

    assert asyncio.run(scenario()) >= 0.19  # one failure at a time


@pytest.mark.anyio
async def test_concurrent_login_guesses_are_checked_one_per_delay(tmp_path, monkeypatch) -> None:
    import translator_app.routers.system as system

    delay = 0.2
    app = create_app(make_settings(tmp_path, ui_auth="1", ui_user="keei", ui_password="s3cret-pass"))
    app.state.login_throttle = LoginThrottle(delay=delay)
    checks: list[tuple[float, bool]] = []
    real_check = system.check_credentials

    def counting(settings, username, password):  # noqa: ANN001, ANN202
        ok = real_check(settings, username, password)
        checks.append((time.perf_counter(), ok))
        return ok

    monkeypatch.setattr(system, "check_credentials", counting)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        def attempt(password: str) -> asyncio.Task:
            return asyncio.create_task(client.post("/api/login", json={"username": "keei", "password": password}))

        guesses = [attempt(f"guess-{i}") for i in range(8)]
        right = attempt("s3cret-pass")
        await asyncio.sleep(delay * 2.5)
        assert len(checks) <= 3  # the password is checked under the lock: not all nine at once
        responses = await asyncio.gather(*guesses)
        accepted = await right
    assert [r.status_code for r in responses] == [401] * 8 and accepted.status_code == 200
    assert len(checks) == 9 and sum(ok for _, ok in checks) == 1
    gaps_after_failures = [later - earlier for (earlier, ok), (later, _) in zip(checks, checks[1:]) if not ok]
    assert gaps_after_failures and min(gaps_after_failures) >= delay * 0.9
    assert LoginThrottle().delay == 1.0  # production: about one guess per second for all clients together


def test_login_without_auth_is_a_no_op(mock_client) -> None:
    assert mock_client.post("/api/login", json={"username": "x", "password": "y"}).json() == {"ok": True, "user": None}
    assert mock_client.get("/login", follow_redirects=False).status_code == 302


def test_exempt_paths() -> None:
    assert is_exempt("/static/login.js") and is_exempt("/api/login") and not is_exempt("/api/glossaries")
