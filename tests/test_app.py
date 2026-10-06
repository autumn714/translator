from fastapi.testclient import TestClient

from conftest import make_settings
from translator_app.main import create_app


def test_index_injects_backend_language_metadata(tmp_path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "window.TRANSLATOR_LANGUAGES =" in response.text
    assert "__TRANSLATOR_LANGUAGES__" not in response.text
    assert "터키어" in response.text
    assert "튀르키예어" not in response.text


def test_app_starts_with_default_openai_engine_without_contacting_the_model(tmp_path) -> None:
    settings = make_settings(tmp_path, engine_type="openai_compatible", llm_base_url="http://127.0.0.1:9/v1")
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.app.state.llm is not None
        assert client.app.state.translator.engine_name == "openai_compatible"
