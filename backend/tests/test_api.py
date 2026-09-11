from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_health():
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_rejects_unsupported_file():
    response = client.post(
        "/api/v1/documents/process",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        data={"document_type": "invoice"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "UNSUPPORTED_FILE_TYPE"


def test_health_reports_current_build():
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["build_version"] == "v11.3-gemini-vision"


def test_gemini_configuration_defaults(monkeypatch):
    from app.core.config import Settings
    settings = Settings(_env_file=None)
    assert settings.gemini_model == "gemini-3.5-flash"
    assert settings.gemini_timeout_seconds == 120
