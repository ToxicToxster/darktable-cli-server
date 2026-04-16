from pathlib import Path
from subprocess import CompletedProcess

from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_ok(monkeypatch):
    monkeypatch.setattr("app.main.get_darktable_cli_path", lambda: "/usr/bin/darktable-cli")
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["darktable_cli_available"] is True


def test_render_rejects_invalid_parameter(monkeypatch):
    monkeypatch.setattr("app.main.get_darktable_cli_path", lambda: "/usr/bin/darktable-cli")
    client = TestClient(app)

    response = client.post(
        "/render",
        data={"width": "0"},
        files={"file": ("image.dng", b"rawdata", "application/octet-stream")},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "Ungueltige Anfrageparameter"


def test_render_rejects_unsupported_extension(monkeypatch):
    monkeypatch.setattr("app.main.get_darktable_cli_path", lambda: "/usr/bin/darktable-cli")
    client = TestClient(app)

    response = client.post(
        "/render",
        files={"file": ("image.txt", b"not-a-raw", "text/plain")},
    )

    assert response.status_code == 415
    assert response.json()["error"] == "Nicht unterstuetzter Dateityp"


def test_render_success_returns_jpeg(monkeypatch):
    monkeypatch.setattr("app.main.get_darktable_cli_path", lambda: "/usr/bin/darktable-cli")

    def fake_run(cmd, stdout, stderr, text, timeout, check):
        output_path = Path(cmd[2])
        output_path.write_bytes(b"fake-jpeg-data")
        return CompletedProcess(args=cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("app.main.subprocess.run", fake_run)
    client = TestClient(app)

    response = client.post(
        "/render",
        files={"file": ("image.dng", b"rawdata", "application/octet-stream")},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.headers["content-disposition"] == 'inline; filename="preview.jpg"'
    assert response.content == b"fake-jpeg-data"
