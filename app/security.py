"""Security utilities: middlewares, filename sanitization, input validation."""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections import defaultdict
from ipaddress import IPv4Network, IPv6Network
from pathlib import PurePosixPath
from typing import Sequence

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.models import build_error_payload

logger = logging.getLogger("darktable_server.security")

# ---------------------------------------------------------------------------
# Filename sanitization
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Parameter validators
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# dt_arg / dt_conf safety
# ---------------------------------------------------------------------------

_DANGEROUS_ARG_PATTERNS = re.compile(
    r"^(--|)("
    r"output|input|"
    r"out|"
    r"help|version"
    r")$",
    re.IGNORECASE,
)


def validate_dt_arg(token: str) -> str:
    """Validate a single extra darktable-cli argv token.

    Level 2 blocks dt_arg entirely (handled in endpoint code).
    Level 3 allows them, but we still validate for safety.
    """
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


# ---------------------------------------------------------------------------
# Always-on hardening middleware
# ---------------------------------------------------------------------------

_OPEN_PATHS = frozenset(("/health", "/version", "/docs", "/redoc", "/openapi.json"))


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
                        content=build_error_payload(
                            f"Upload exceeds maximum size of {self._max} bytes",
                        ),
                    )
            except ValueError:
                pass
        return await call_next(request)


# ---------------------------------------------------------------------------
# Access-security middlewares (conditional, gated by effective config)
# ---------------------------------------------------------------------------

class APIKeyMiddleware(BaseHTTPMiddleware):
    """Require ``X-API-Key`` header on non-open endpoints."""

    def __init__(self, app: object, api_key: str) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._api_key = api_key

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path.rstrip("/")
        if path in _OPEN_PATHS:
            return await call_next(request)
        provided = request.headers.get("X-API-Key")
        if provided != self._api_key:
            return JSONResponse(
                status_code=401,
                content=build_error_payload("Invalid or missing API key"),
            )
        return await call_next(request)


class LocalhostOnlyMiddleware(BaseHTTPMiddleware):
    """Allow only loopback addresses (direct socket IP, no proxy trust)."""

    _LOOPBACK = frozenset(("127.0.0.1", "::1"))

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path.rstrip("/")
        if path in _OPEN_PATHS:
            return await call_next(request)
        client_ip = request.client.host if request.client else None
        if client_ip not in self._LOOPBACK:
            return JSONResponse(
                status_code=403,
                content=build_error_payload("Access restricted to localhost"),
            )
        return await call_next(request)


class IPAllowlistMiddleware(BaseHTTPMiddleware):
    """Allow only IPs matching configured networks (direct socket IP, no proxy trust)."""

    def __init__(
        self, app: object, networks: Sequence[IPv4Network | IPv6Network],
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._networks = list(networks)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path.rstrip("/")
        if path in _OPEN_PATHS:
            return await call_next(request)
        client_ip = request.client.host if request.client else None
        if client_ip is None:
            return JSONResponse(status_code=403, content=build_error_payload("No client IP"))
        from ipaddress import ip_address
        try:
            addr = ip_address(client_ip)
        except ValueError:
            return JSONResponse(
                status_code=403,
                content=build_error_payload("Invalid client IP"),
            )
        for net in self._networks:
            if addr in net:
                return await call_next(request)
        return JSONResponse(
            status_code=403,
            content=build_error_payload("IP address not in allowlist"),
        )


class RateLimitMiddleware(BaseHTTPMiddleware):
    """In-memory sliding-window rate limiter per client IP.

    Single-worker only. For multi-worker deployments, use an external
    rate-limiter (e.g. nginx, Traefik, or a Redis-backed solution).
    """

    def __init__(self, app: object, rpm: int) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._rpm = rpm
        self._window = 60.0
        self._requests: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path.rstrip("/")
        if path in _OPEN_PATHS:
            return await call_next(request)
        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        cutoff = now - self._window
        # prune old entries
        bucket = self._requests[client_ip]
        self._requests[client_ip] = [t for t in bucket if t > cutoff]
        bucket = self._requests[client_ip]
        if len(bucket) >= self._rpm:
            return JSONResponse(
                status_code=429,
                content=build_error_payload("Rate limit exceeded"),
            )
        bucket.append(now)
        return await call_next(request)
