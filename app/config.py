"""Application configuration with env / .env / CLI / defaults precedence."""

from __future__ import annotations

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

    # --- advanced arg safety ---
    dt_arg_denylist: str = ""

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, v: str) -> str:
        v = v.upper()
        if v not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError(f"Invalid log_level: {v}")
        return v

    # --- derived helpers (not stored, computed) ---

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

    def dt_arg_denylist_set(self) -> set[str]:
        return {t.strip().lower() for t in self.dt_arg_denylist.split(",") if t.strip()}


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
