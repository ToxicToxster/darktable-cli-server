"""Security utilities: API key auth, filename sanitization, input validation."""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import PurePosixPath
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.config import Settings

logger = logging.getLogger("darktable_server.security")

# --- filename sanitization ---

_SAFE_FILENAME_RE = re.compile(r"[^\w\-.]")


def sanitize_filename(name: str) -> str:
    """Return a safe ASCII-only basename without path components."""
    name = PurePosixPath(name).name
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = _SAFE_FILENAME_RE.sub("_", name)
    name = name.strip("_. ")
    return name or "output"


def derive_output_filename(original_filename: str, output_ext: str) -> str:
    """Replace extension of the original upload name with *output_ext*."""
    base = sanitize_filename(original_filename)
    stem = PurePosixPath(base).stem or "output"
    ext = output_ext.lower().lstrip(".")
    return f"{stem}.{ext}"


# --- parameter validators ---

def validate_int(name: str, value: str | int, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{name}' must be an integer") from None
    if v < lo or v > hi:
        raise ValueError(f"'{name}' must be between {lo} and {hi}")
    return v


def validate_bool(name: str, value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"'{name}' must be a boolean (true/false/1/0)")


def validate_output_format(fmt: str, allowed: set[str]) -> str:
    fmt = fmt.strip().lower()
    if fmt not in allowed:
        raise ValueError(f"Unsupported output format '{fmt}'. Allowed: {sorted(allowed)}")
    return fmt


# --- dt_arg / dt_conf safety ---

_DANGEROUS_ARG_PATTERNS = re.compile(
    r"^(--|)("
    r"output|input|"          # must not override file paths
    r"out|"
    r"help|version"
    r")$",
    re.IGNORECASE,
)


def validate_dt_arg(token: str, denylist: set[str]) -> str:
    """Validate a single extra darktable-cli argv token."""
    token = token.strip()
    if not token:
        raise ValueError("Empty dt_arg token")
    if len(token) > 256:
        raise ValueError("dt_arg token too long")
    if "\x00" in token or "\n" in token:
        raise ValueError("dt_arg contains forbidden characters")
    low = token.lower()
    if _DANGEROUS_ARG_PATTERNS.match(low):
        raise ValueError(f"dt_arg '{token}' is not allowed (reserved)")
    if denylist and low in denylist:
        raise ValueError(f"dt_arg '{token}' is denied by server policy")
    return token


def validate_dt_conf(token: str) -> str:
    """Validate a darktable --conf key=value token."""
    token = token.strip()
    if not token:
        raise ValueError("Empty dt_conf value")
    if len(token) > 512:
        raise ValueError("dt_conf value too long")
    if "\x00" in token or "\n" in token:
        raise ValueError("dt_conf contains forbidden characters")
    if "=" not in token:
        raise ValueError("dt_conf must be in 'key=value' format")
    return token


# --- API key middleware ---

class APIKeyMiddleware(BaseHTTPMiddleware):
    """If ``api_key`` is configured, require ``X-API-Key`` header on all non-health endpoints."""

    def __init__(self, app: object, settings: Settings) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._api_key: Optional[str] = settings.api_key

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if self._api_key is None:
            return await call_next(request)
        path = request.url.path.rstrip("/")
        if path in ("/health", "/version", "/docs", "/redoc", "/openapi.json"):
            return await call_next(request)
        provided = request.headers.get("X-API-Key")
        if provided != self._api_key:
            return JSONResponse(status_code=401, content={"error": "Invalid or missing API key"})
        return await call_next(request)


# --- upload-size middleware ---

class MaxUploadSizeMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Content-Length exceeds the configured maximum."""

    def __init__(self, app: object, max_bytes: int) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._max = max_bytes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > self._max:
                    return JSONResponse(
                        status_code=413,
                        content={"error": f"Upload exceeds maximum size of {self._max} bytes"},
                    )
            except ValueError:
                pass
        return await call_next(request)
