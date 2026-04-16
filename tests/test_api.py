"""Tests for validation, command building, and API endpoints."""

from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security import (
    derive_output_filename,
    sanitize_filename,
    validate_bool,
    validate_dt_arg,
    validate_dt_conf,
    validate_int,
    validate_output_format,
)
from app.services.darktable import RenderParams, build_command


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def settings() -> Settings:
    return Settings(
        api_key=None,
        temp_dir="/tmp/dt-test",
        max_upload_bytes=10 * 1024 * 1024,
        request_timeout=30,
        max_concurrent_renders=2,
    )


@pytest.fixture()
def client(settings: Settings) -> TestClient:
    app = create_app(settings)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Filename sanitization
# ---------------------------------------------------------------------------

class TestSanitizeFilename:
    def test_simple(self) -> None:
        assert sanitize_filename("IMG_001.dng") == "IMG_001.dng"

    def test_path_traversal(self) -> None:
        assert sanitize_filename("../../etc/passwd") == "passwd"

    def test_unicode(self) -> None:
        result = sanitize_filename("foto\u00fc\u00e4\u00f6.dng")
        assert ".." not in result
        assert "/" not in result

    def test_empty(self) -> None:
        assert sanitize_filename("") == "output"


class TestDeriveOutputFilename:
    def test_replaces_extension(self) -> None:
        assert derive_output_filename("IMG20251231.dng", "jpg") == "IMG20251231.jpg"

    def test_preserves_stem(self) -> None:
        assert derive_output_filename("photo.arw", "png") == "photo.png"


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------

class TestValidateInt:
    def test_valid(self) -> None:
        assert validate_int("w", "2000", 0, 16000) == 2000

    def test_zero(self) -> None:
        assert validate_int("w", "0", 0, 16000) == 0

    def test_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="between"):
            validate_int("w", "20000", 0, 16000)

    def test_non_integer(self) -> None:
        with pytest.raises(ValueError, match="integer"):
            validate_int("w", "abc", 0, 16000)


class TestValidateBool:
    @pytest.mark.parametrize("v", ["true", "1", "yes", "on", "True"])
    def test_truthy(self, v: str) -> None:
        assert validate_bool("hq", v) is True

    @pytest.mark.parametrize("v", ["false", "0", "no", "off", "False"])
    def test_falsy(self, v: str) -> None:
        assert validate_bool("hq", v) is False

    def test_invalid(self) -> None:
        with pytest.raises(ValueError):
            validate_bool("hq", "maybe")


class TestValidateOutputFormat:
    def test_valid(self) -> None:
        assert validate_output_format("jpg", {"jpg", "png"}) == "jpg"

    def test_invalid(self) -> None:
        with pytest.raises(ValueError, match="Unsupported"):
            validate_output_format("bmp", {"jpg", "png"})


class TestValidateDtArg:
    def test_valid_token(self) -> None:
        assert validate_dt_arg("--verbose", set()) == "--verbose"

    def test_rejects_output(self) -> None:
        with pytest.raises(ValueError, match="reserved"):
            validate_dt_arg("output", set())

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="Empty"):
            validate_dt_arg("", set())

    def test_rejects_null_byte(self) -> None:
        with pytest.raises(ValueError, match="forbidden"):
            validate_dt_arg("foo\x00bar", set())

    def test_denylist(self) -> None:
        with pytest.raises(ValueError, match="denied"):
            validate_dt_arg("--banned", {"--banned"})


class TestValidateDtConf:
    def test_valid(self) -> None:
        assert validate_dt_conf("plugins/foo/bar=42") == "plugins/foo/bar=42"

    def test_missing_equals(self) -> None:
        with pytest.raises(ValueError, match="key=value"):
            validate_dt_conf("nope")


# ---------------------------------------------------------------------------
# Command building
# ---------------------------------------------------------------------------

class TestBuildCommand:
    def test_basic_jpg(self) -> None:
        params = RenderParams(width=2000, height=1500, quality=90)
        cmd = build_command("/usr/bin/darktable-cli", Path("/tmp/in.dng"), Path("/tmp/out.jpg"), params)
        assert cmd[0] == "/usr/bin/darktable-cli"
        assert cmd[1] == "/tmp/in.dng"
        assert cmd[2] == "/tmp/out.jpg"
        assert "--width" in cmd
        assert "2000" in cmd
        assert "--height" in cmd
        assert "1500" in cmd
        assert "--core" in cmd
        assert "plugins/imageio/format/jpeg/quality=90" in cmd

    def test_no_width_height_when_none(self) -> None:
        params = RenderParams(width=None, height=None, quality=80)
        cmd = build_command("/usr/bin/darktable-cli", Path("/tmp/in.dng"), Path("/tmp/out.jpg"), params)
        assert "--width" not in cmd
        assert "--height" not in cmd

    def test_extra_args_and_confs(self) -> None:
        params = RenderParams(
            output_format="png",
            quality=80,
            extra_args=["--verbose"],
            extra_confs=["plugins/foo=bar"],
        )
        cmd = build_command("/usr/bin/darktable-cli", Path("/tmp/in.dng"), Path("/tmp/out.png"), params)
        assert "--verbose" in cmd
        assert "plugins/foo=bar" in cmd

    def test_always_list_never_shell(self) -> None:
        params = RenderParams(quality=80)
        cmd = build_command("/usr/bin/darktable-cli", Path("/tmp/in.dng"), Path("/tmp/out.jpg"), params)
        assert isinstance(cmd, list)
        for token in cmd:
            assert isinstance(token, str)


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    def test_health(self, client: TestClient) -> None:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestVersionEndpoint:
    def test_version(self, client: TestClient) -> None:
        r = client.get("/version")
        assert r.status_code == 200
        data = r.json()
        assert "app_version" in data
        assert "python_version" in data


class TestRenderValidation:
    def test_rejects_unsupported_extension(self, client: TestClient) -> None:
        r = client.post("/render", files={"file": ("test.txt", b"data", "text/plain")})
        assert r.status_code == 415

    def test_rejects_invalid_width(self, client: TestClient) -> None:
        r = client.post(
            "/render",
            data={"width": "abc"},
            files={"file": ("test.dng", b"data", "application/octet-stream")},
        )
        assert r.status_code == 400

    def test_rejects_invalid_format(self, client: TestClient) -> None:
        r = client.post(
            "/render",
            data={"output_format": "bmp"},
            files={"file": ("test.dng", b"data", "application/octet-stream")},
        )
        assert r.status_code == 400


class TestPreviewValidation:
    def test_rejects_unsupported_extension(self, client: TestClient) -> None:
        r = client.post("/preview", files={"file": ("test.gif", b"data", "image/gif")})
        assert r.status_code == 415


class TestRenderSuccess:
    def test_render_returns_jpeg(self, client: TestClient) -> None:
        def fake_run(cmd, stdout, stderr, text, timeout, check):
            Path(cmd[2]).write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
            return CompletedProcess(args=cmd, returncode=0, stdout="ok", stderr="")

        with patch("app.services.darktable.subprocess.run", fake_run), \
             patch("app.services.darktable.get_darktable_cli_path", return_value="/usr/bin/darktable-cli"):
            r = client.post(
                "/render",
                files={"file": ("IMG_001.dng", b"raw-data", "application/octet-stream")},
                data={"quality": "90"},
            )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/jpeg")
        assert "IMG_001.jpg" in r.headers.get("content-disposition", "")

    def test_preview_returns_jpeg(self, client: TestClient) -> None:
        def fake_run(cmd, stdout, stderr, text, timeout, check):
            Path(cmd[2]).write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
            return CompletedProcess(args=cmd, returncode=0, stdout="ok", stderr="")

        with patch("app.services.darktable.subprocess.run", fake_run), \
             patch("app.services.darktable.get_darktable_cli_path", return_value="/usr/bin/darktable-cli"):
            r = client.post(
                "/preview",
                files={"file": ("IMG_001.arw", b"raw-data", "application/octet-stream")},
            )
        assert r.status_code == 200
        assert "IMG_001.jpg" in r.headers.get("content-disposition", "")


class TestAPIKeyAuth:
    def test_rejected_without_key(self) -> None:
        s = Settings(api_key="secret123", temp_dir="/tmp/dt-test-auth")
        a = create_app(s)
        c = TestClient(a)
        r = c.post("/render", files={"file": ("test.dng", b"data", "application/octet-stream")})
        assert r.status_code == 401

    def test_accepted_with_key(self) -> None:
        s = Settings(api_key="secret123", temp_dir="/tmp/dt-test-auth")
        a = create_app(s)
        c = TestClient(a)
        r = c.post(
            "/render",
            files={"file": ("test.dng", b"data", "application/octet-stream")},
            headers={"X-API-Key": "secret123"},
        )
        assert r.status_code != 401

    def test_health_no_auth(self) -> None:
        s = Settings(api_key="secret123", temp_dir="/tmp/dt-test-auth")
        a = create_app(s)
        c = TestClient(a)
        r = c.get("/health")
        assert r.status_code == 200
