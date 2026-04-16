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


def _raw_render(
    client: TestClient,
    filename: str = "test.dng",
    body: bytes = b"raw-data",
    params: dict | None = None,
    extra_headers: dict | None = None,
) -> "TestClient":
    """Helper: POST /render with raw body + X-Filename + query params."""
    headers = {"Content-Type": "application/octet-stream", "X-Filename": filename}
    if extra_headers:
        headers.update(extra_headers)
    url = "/render"
    if params:
        from urllib.parse import urlencode
        url = f"/render?{urlencode(params, doseq=True)}"
    return client.post(url, content=body, headers=headers)


def _raw_preview(
    client: TestClient,
    filename: str = "test.dng",
    body: bytes = b"raw-data",
    extra_headers: dict | None = None,
) -> "TestClient":
    """Helper: POST /preview with raw body + X-Filename."""
    headers = {"Content-Type": "application/octet-stream", "X-Filename": filename}
    if extra_headers:
        headers.update(extra_headers)
    return client.post("/preview", content=body, headers=headers)


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
        assert validate_dt_arg("--verbose") == "--verbose"

    def test_rejects_output(self) -> None:
        with pytest.raises(ValueError, match="reserved"):
            validate_dt_arg("output")

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="Empty"):
            validate_dt_arg("")

    def test_rejects_null_byte(self) -> None:
        with pytest.raises(ValueError, match="forbidden"):
            validate_dt_arg("foo\x00bar")


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
# API endpoint tests — health & version
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


# ---------------------------------------------------------------------------
# /render validation (raw binary transport)
# ---------------------------------------------------------------------------

class TestRenderValidation:
    def test_rejects_missing_x_filename(self, client: TestClient) -> None:
        r = client.post("/render", content=b"raw-data",
                        headers={"Content-Type": "application/octet-stream"})
        assert r.status_code == 400
        assert "X-Filename" in r.json()["detail"]

    def test_rejects_unsupported_extension(self, client: TestClient) -> None:
        r = _raw_render(client, filename="test.txt")
        assert r.status_code == 415

    def test_rejects_invalid_width(self, client: TestClient) -> None:
        r = _raw_render(client, params={"width": "abc"})
        assert r.status_code == 400

    def test_rejects_invalid_format(self, client: TestClient) -> None:
        r = _raw_render(client, params={"output_format": "bmp"})
        assert r.status_code == 400

    def test_rejects_empty_body(self, client: TestClient) -> None:
        r = _raw_render(client, body=b"")
        assert r.status_code == 400

    def test_rejects_invalid_quality(self, client: TestClient) -> None:
        r = _raw_render(client, params={"quality": "200"})
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# /preview validation (raw binary transport)
# ---------------------------------------------------------------------------

class TestPreviewValidation:
    def test_rejects_missing_x_filename(self, client: TestClient) -> None:
        r = client.post("/preview", content=b"raw-data",
                        headers={"Content-Type": "application/octet-stream"})
        assert r.status_code == 400
        assert "X-Filename" in r.json()["detail"]

    def test_rejects_empty_x_filename(self, client: TestClient) -> None:
        r = _raw_preview(client, filename="  ")
        assert r.status_code == 400

    def test_rejects_unsupported_extension(self, client: TestClient) -> None:
        r = _raw_preview(client, filename="test.gif")
        assert r.status_code == 415

    def test_rejects_empty_body(self, client: TestClient) -> None:
        r = _raw_preview(client, body=b"")
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Successful renders (mocked darktable-cli)
# ---------------------------------------------------------------------------

def _fake_run(cmd, stdout, stderr, text, timeout, check):
    Path(cmd[2]).write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    return CompletedProcess(args=cmd, returncode=0, stdout="ok", stderr="")


_DTCLI_PATCH = patch("app.services.darktable.subprocess.run", _fake_run)
_DTPATH_PATCH = patch(
    "app.services.darktable.get_darktable_cli_path",
    return_value="/usr/bin/darktable-cli",
)


class TestRenderSuccess:
    def test_render_returns_jpeg(self, client: TestClient) -> None:
        with _DTCLI_PATCH, _DTPATH_PATCH:
            r = _raw_render(client, filename="IMG_001.dng", params={"quality": "90"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/jpeg")
        assert "IMG_001.jpg" in r.headers.get("content-disposition", "")

    def test_render_png(self, client: TestClient) -> None:
        def fake_png(cmd, stdout, stderr, text, timeout, check):
            Path(cmd[2]).write_bytes(b"\x89PNGfake")
            return CompletedProcess(args=cmd, returncode=0, stdout="ok", stderr="")

        with patch("app.services.darktable.subprocess.run", fake_png), _DTPATH_PATCH:
            r = _raw_render(client, filename="test.cr2", params={"output_format": "png"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/png")


class TestPreviewSuccess:
    def test_preview_returns_jpeg(self, client: TestClient) -> None:
        with _DTCLI_PATCH, _DTPATH_PATCH:
            r = _raw_preview(client, filename="IMG_001.arw")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/jpeg")
        assert "IMG_001.jpg" in r.headers.get("content-disposition", "")


# ---------------------------------------------------------------------------
# Security levels
# ---------------------------------------------------------------------------

class TestSecurityLevels:
    """Test SECURITY_LEVEL gating on /render."""

    def test_level1_blocks_render(self) -> None:
        s = Settings(security_level=1, temp_dir="/tmp/dt-sec1")
        c = TestClient(create_app(s))
        r = _raw_render(c, filename="test.dng")
        assert r.status_code == 403
        assert "disabled" in r.json()["detail"].lower()

    def test_level1_allows_preview(self) -> None:
        s = Settings(security_level=1, temp_dir="/tmp/dt-sec1")
        c = TestClient(create_app(s))
        with _DTCLI_PATCH, _DTPATH_PATCH:
            r = _raw_preview(c, filename="test.dng")
        assert r.status_code == 200

    def test_level2_allows_render(self) -> None:
        s = Settings(security_level=2, temp_dir="/tmp/dt-sec2")
        c = TestClient(create_app(s))
        with _DTCLI_PATCH, _DTPATH_PATCH:
            r = _raw_render(c, filename="test.dng")
        assert r.status_code == 200

    def test_level2_blocks_dt_arg(self) -> None:
        s = Settings(security_level=2, temp_dir="/tmp/dt-sec2")
        c = TestClient(create_app(s))
        r = _raw_render(c, filename="test.dng", params={"dt_arg": "--verbose"})
        assert r.status_code == 403
        assert "passthrough" in r.json()["detail"].lower()

    def test_level2_blocks_dt_conf(self) -> None:
        s = Settings(security_level=2, temp_dir="/tmp/dt-sec2")
        c = TestClient(create_app(s))
        r = _raw_render(c, filename="test.dng", params={"dt_conf": "key=val"})
        assert r.status_code == 403

    def test_level3_allows_dt_arg(self) -> None:
        s = Settings(
            security_level=3, temp_dir="/tmp/dt-sec3",
            access_security_enabled=True, access_require_api_key=False,
        )
        c = TestClient(create_app(s))
        with _DTCLI_PATCH, _DTPATH_PATCH:
            r = _raw_render(c, filename="test.dng", params={"dt_arg": "--verbose"})
        assert r.status_code == 200

    def test_level3_allows_dt_conf(self) -> None:
        s = Settings(
            security_level=3, temp_dir="/tmp/dt-sec3",
            access_security_enabled=True, access_require_api_key=False,
        )
        c = TestClient(create_app(s))
        with _DTCLI_PATCH, _DTPATH_PATCH:
            r = _raw_render(c, filename="test.dng", params={"dt_conf": "key=val"})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

class TestStartupValidation:
    def test_level3_requires_access_security(self) -> None:
        """Level 3 forces effective_access_security = True, which is fine (no crash)."""
        s = Settings(
            security_level=3, temp_dir="/tmp/dt-val",
            access_security_enabled=True, access_require_api_key=False,
        )
        # Should not raise
        s.validate_effective_config()

    def test_api_key_required_but_missing_raises(self) -> None:
        s = Settings(
            security_level=2, temp_dir="/tmp/dt-val",
            access_security_enabled=True,
            access_require_api_key=True,
            api_key=None,
        )
        with pytest.raises(ValueError, match="API_KEY"):
            s.validate_effective_config()

    def test_ip_allowlist_enabled_but_empty_raises(self) -> None:
        s = Settings(
            security_level=2, temp_dir="/tmp/dt-val",
            access_security_enabled=True,
            access_enable_ip_allowlist=True,
            access_ip_allowlist="",
        )
        with pytest.raises(ValueError, match="(?i)allowlist"):
            s.validate_effective_config()


# ---------------------------------------------------------------------------
# Access-security features
# ---------------------------------------------------------------------------

class TestAccessSecurity:
    def test_api_key_rejected_without_key(self) -> None:
        s = Settings(
            api_key="secret123", temp_dir="/tmp/dt-acc",
            access_security_enabled=True, access_require_api_key=True,
        )
        c = TestClient(create_app(s))
        r = _raw_render(c, filename="test.dng")
        assert r.status_code == 401

    def test_api_key_accepted_with_key(self) -> None:
        s = Settings(
            api_key="secret123", temp_dir="/tmp/dt-acc",
            access_security_enabled=True, access_require_api_key=True,
        )
        c = TestClient(create_app(s))
        with _DTCLI_PATCH, _DTPATH_PATCH:
            r = _raw_render(c, filename="test.dng", extra_headers={"X-API-Key": "secret123"})
        assert r.status_code == 200

    def test_health_no_auth_required(self) -> None:
        s = Settings(
            api_key="secret123", temp_dir="/tmp/dt-acc",
            access_security_enabled=True, access_require_api_key=True,
        )
        c = TestClient(create_app(s))
        r = c.get("/health")
        assert r.status_code == 200

    def test_api_key_inactive_without_access_security(self) -> None:
        """API key set but access_security_enabled=False → no middleware → allowed."""
        s = Settings(
            api_key="secret123", temp_dir="/tmp/dt-acc",
            access_security_enabled=False, access_require_api_key=True,
        )
        c = TestClient(create_app(s))
        with _DTCLI_PATCH, _DTPATH_PATCH:
            r = _raw_render(c, filename="test.dng")
        # Should NOT be 401 because access security is off
        assert r.status_code != 401
