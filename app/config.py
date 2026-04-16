"""Application configuration with env / .env / CLI / defaults precedence."""

from __future__ import annotations

import ipaddress
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings

_APP_VERSION = "1.0.0"


class Settings(BaseSettings):
    """All configurable knobs for darktable-cli-server.

    Precedence (highest first): CLI args > env vars > .env file > defaults.
    Pydantic-settings handles env and .env automatically; CLI override is done
    in ``__main__`` / uvicorn CLI.
    """

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # --- server ---
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"

    # --- limits ---
    max_upload_bytes: int = 200 * 1024 * 1024  # 200 MB
    request_timeout: int = 120  # seconds for darktable-cli
    max_concurrent_renders: int = 4

    # --- paths ---
    temp_dir: str = "/tmp/darktable-work"

    # --- auth ---
    api_key: Optional[str] = None

    # --- security level ---
    security_level: int = 2

    # --- preview defaults ---
    preview_format: str = "jpg"
    preview_quality: int = 80
    preview_width: int = 2000
    preview_height: int = 2000
    preview_hq: bool = False
    preview_upscale: bool = False
    preview_apply_custom_presets: bool = False

    # --- format allow-lists ---
    allowed_output_formats: str = "jpg,jpeg,png,tif,tiff"
    allowed_raw_extensions: str = ".dng,.arw,.nef,.cr2,.cr3,.orf,.rw2,.raf,.pef,.srw"

    # --- access security ---
    access_security_enabled: bool = False
    access_require_api_key: bool = False
    access_localhost_only: bool = False
    access_enable_ip_allowlist: bool = False
    access_ip_allowlist: str = ""
    access_enable_cors_restriction: bool = False
    access_cors_allowed_origins: str = ""
    access_enable_rate_limit: bool = False
    access_rate_limit_rpm: int = 60

    # --- validators ---

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, v: str) -> str:
        v = v.upper()
        if v not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError(f"Invalid log_level: {v}")
        return v

    @field_validator("security_level")
    @classmethod
    def _validate_security_level(cls, v: int) -> int:
        if v not in (1, 2, 3):
            raise ValueError("SECURITY_LEVEL must be 1, 2, or 3")
        return v

    # --- permission helpers ---

    def is_render_allowed(self) -> bool:
        return self.security_level >= 2

    def is_render_passthrough_allowed(self) -> bool:
        return self.security_level == 3

    def effective_access_security_enabled(self) -> bool:
        return True if self.security_level == 3 else self.access_security_enabled

    # --- derived helpers ---

    def allowed_output_formats_set(self) -> set[str]:
        return {f.strip().lower() for f in self.allowed_output_formats.split(",") if f.strip()}

    def allowed_raw_extensions_set(self) -> set[str]:
        exts: set[str] = set()
        for e in self.allowed_raw_extensions.split(","):
            e = e.strip().lower()
            if e and not e.startswith("."):
                e = f".{e}"
            if e:
                exts.add(e)
        return exts

    def parsed_ip_allowlist(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for entry in self.access_ip_allowlist.split(","):
            entry = entry.strip()
            if entry:
                networks.append(ipaddress.ip_network(entry, strict=False))
        return networks

    def parsed_cors_origins(self) -> list[str]:
        return [o.strip() for o in self.access_cors_allowed_origins.split(",") if o.strip()]

    # --- startup validation ---

    def validate_effective_config(self) -> None:
        eff = self.effective_access_security_enabled()
        if eff and self.access_require_api_key:
            if not self.api_key:
                raise ValueError(
                    "ACCESS_REQUIRE_API_KEY is enabled but no API_KEY is configured"
                )
        if eff and self.access_enable_ip_allowlist:
            raw = self.access_ip_allowlist.strip()
            if not raw:
                raise ValueError(
                    "ACCESS_ENABLE_IP_ALLOWLIST is enabled but ACCESS_IP_ALLOWLIST is empty"
                )
            for entry in raw.split(","):
                entry = entry.strip()
                if not entry:
                    continue
                try:
                    ipaddress.ip_network(entry, strict=False)
                except ValueError as exc:
                    raise ValueError(
                        f"ACCESS_IP_ALLOWLIST contains invalid entry '{entry}': {exc}"
                    ) from exc
        if eff and self.access_enable_cors_restriction:
            if not self.access_cors_allowed_origins.strip():
                raise ValueError(
                    "ACCESS_ENABLE_CORS_RESTRICTION is enabled but "
                    "ACCESS_CORS_ALLOWED_ORIGINS is empty"
                )
        if eff and self.access_enable_rate_limit:
            if self.access_rate_limit_rpm < 1:
                raise ValueError(
                    "ACCESS_RATE_LIMIT_RPM must be >= 1 when rate limiting is enabled"
                )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def get_app_version() -> str:
    return _APP_VERSION


def get_darktable_version() -> Optional[str]:
    cli = shutil.which("darktable-cli")
    if cli is None:
        return None
    try:
        result = subprocess.run(
            [cli, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in (result.stdout + result.stderr).splitlines():
            if "darktable" in line.lower():
                return line.strip()
        return (result.stdout + result.stderr).strip()[:200] or None
    except Exception:
        return None
