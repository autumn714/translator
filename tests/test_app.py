from fastapi.testclient import TestClient

from translator_app.main import create_app


def test_index_injects_backend_language_metadata() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "window.TRANSLATOR_LANGUAGES =" in response.text
    assert "__TRANSLATOR_LANGUAGES__" not in response.text
    assert "터키어" in response.text
    assert "튀르키예어" not in response.text
