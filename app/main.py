"""darktable-cli-server: production-grade HTTP wrapper around darktable-cli."""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from app.config import Settings, get_app_version, get_darktable_version, get_settings
from app.deps import get_semaphore, init_semaphore
from app.models import ErrorResponse, HealthResponse, VersionResponse
from app.security import (
    APIKeyMiddleware,
    IPAllowlistMiddleware,
    LocalhostOnlyMiddleware,
    MaxUploadSizeMiddleware,
    RateLimitMiddleware,
    derive_output_filename,
    sanitize_filename,
    validate_bool,
    validate_dt_arg,
    validate_dt_conf,
    validate_int,
    validate_output_format,
)
from app.services.darktable import FORMAT_EXTENSION_MAP, RenderParams, get_darktable_cli_path, run_render
from app.services.files import cleanup_temp_dir, create_temp_dir, write_body_to_file

logger = logging.getLogger("darktable_server")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _validate_filename_header(request: Request, settings: Settings) -> tuple[str, str]:
    """Extract and validate X-Filename. Returns (sanitized_name, extension)."""
    raw = request.headers.get("x-filename")
    if not raw or not raw.strip():
        raise HTTPException(status_code=400, detail="Missing or empty X-Filename header")
    filename = sanitize_filename(raw)
    ext = Path(filename).suffix.lower()
    allowed_exts = settings.allowed_raw_extensions_set()
    if ext not in allowed_exts:
        raise HTTPException(status_code=415, detail={
            "error": "Unsupported file type",
            "extension": ext,
            "allowed": sorted(allowed_exts),
        })
    return filename, ext


def _media_type(fmt: str) -> str:
    if fmt in ("jpg", "jpeg"):
        return "image/jpeg"
    if fmt in ("tif", "tiff"):
        return "image/tiff"
    if fmt == "png":
        return "image/png"
    return f"image/{fmt}"


async def _render_pipeline(
    *,
    request: Request,
    filename: str,
    ext: str,
    params: RenderParams,
    settings: Settings,
    endpoint: str,
) -> FileResponse:
    """Shared render pipeline for both /preview and /render."""
    fmt = params.output_format
    out_ext = FORMAT_EXTENSION_MAP.get(fmt, f".{fmt}")
    temp_dir = create_temp_dir(settings.temp_dir)
    cleanup_needed = True

    try:
        input_path = temp_dir / f"input{ext}"
        output_path = temp_dir / f"output{out_ext}"

        try:
            size = await write_body_to_file(
                request.stream(), input_path, settings.max_upload_bytes,
            )
        except ValueError:
            raise HTTPException(status_code=413, detail="Upload exceeds maximum size")

        if size == 0:
            raise HTTPException(status_code=400, detail="Empty request body")

        sem = get_semaphore()
        try:
            async with asyncio.timeout(settings.request_timeout + 5):
                await sem.acquire()
        except TimeoutError:
            raise HTTPException(status_code=504, detail="Server too busy, render queue full")

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, run_render, input_path, output_path, params,
                settings.request_timeout,
            )
        finally:
            sem.release()

        if not result.success:
            if result.error and "timed out" in result.error.lower():
                raise HTTPException(status_code=504, detail={
                    "error": "Render timeout",
                    "seconds": settings.request_timeout,
                })
            raise HTTPException(status_code=500, detail={
                "error": "Render failed",
                "message": result.error,
                "returncode": result.returncode,
                "stderr": (result.stderr or "")[:1000],
            })

        response_filename = derive_output_filename(filename, out_ext.lstrip("."))
        media = _media_type(fmt)

        logger.info(
            "endpoint=%s format=%s filename=%s duration=%.1fs",
            endpoint, fmt, response_filename, result.duration_seconds,
        )

        cleanup_needed = False
        return FileResponse(
            path=result.output_path,
            media_type=media,
            headers={"Content-Disposition": f'inline; filename="{response_filename}"'},
            background=BackgroundTask(cleanup_temp_dir, temp_dir),
        )
    finally:
        if cleanup_needed:
            cleanup_temp_dir(temp_dir)


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app(settings: Optional[Settings] = None) -> FastAPI:
    if settings is None:
        settings = get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        force=True,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # --- startup validation ---
        settings.validate_effective_config()

        init_semaphore(settings.max_concurrent_renders)
        Path(settings.temp_dir).mkdir(parents=True, exist_ok=True)

        cli = get_darktable_cli_path()
        if cli:
            logger.info("darktable-cli found: %s", cli)
        else:
            logger.warning("darktable-cli NOT found — /render and /preview will fail")

        # --- startup logging ---
        eff = settings.effective_access_security_enabled()
        eff_reason = "forced by level 3" if settings.security_level == 3 else "explicit"
        logger.info(
            "Security config: SECURITY_LEVEL=%d render_allowed=%s "
            "render_passthrough_allowed=%s",
            settings.security_level,
            settings.is_render_allowed(),
            settings.is_render_passthrough_allowed(),
        )
        logger.info(
            "Access security: effective_enabled=%s (%s) "
            "api_key=%s localhost_only=%s ip_allowlist=%s "
            "cors=%s rate_limit=%s",
            eff, eff_reason,
            "active" if eff and settings.access_require_api_key else "inactive",
            "active" if eff and settings.access_localhost_only else "inactive",
            "active" if eff and settings.access_enable_ip_allowlist else "inactive",
            "active" if eff and settings.access_enable_cors_restriction else "inactive",
            "active" if eff and settings.access_enable_rate_limit else "inactive",
        )

        yield

    application = FastAPI(
        title="darktable-cli-server",
        version=get_app_version(),
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # --- always-on hardening middleware ---
    application.add_middleware(MaxUploadSizeMiddleware, max_bytes=settings.max_upload_bytes)

    # --- conditional access-security middleware ---
    eff = settings.effective_access_security_enabled()

    if eff and settings.access_require_api_key and settings.api_key:
        application.add_middleware(APIKeyMiddleware, api_key=settings.api_key)

    if eff and settings.access_localhost_only:
        application.add_middleware(LocalhostOnlyMiddleware)

    if eff and settings.access_enable_ip_allowlist:
        application.add_middleware(
            IPAllowlistMiddleware, networks=settings.parsed_ip_allowlist(),
        )

    if eff and settings.access_enable_rate_limit:
        application.add_middleware(
            RateLimitMiddleware, rpm=settings.access_rate_limit_rpm,
        )

    # CORS is browser-only protection, not a primary access-control mechanism
    if eff and settings.access_enable_cors_restriction:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=settings.parsed_cors_origins(),
            allow_methods=["GET", "POST"],
            allow_headers=["X-API-Key", "X-Filename", "Content-Type"],
        )

    # --- endpoints ---

    @application.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @application.get("/version", response_model=VersionResponse)
    def version() -> VersionResponse:
        return VersionResponse(
            app_version=get_app_version(),
            python_version=sys.version,
            darktable_cli_available=get_darktable_cli_path() is not None,
            darktable_version=get_darktable_version(),
        )

    @application.post(
        "/render",
        response_model=None,
        summary="Flexible render endpoint (raw binary body + query params)",
        description=(
            "Upload a RAW file as the raw HTTP request body. "
            "Pass render parameters as query parameters. "
            "Capabilities depend on the configured SECURITY_LEVEL."
        ),
        responses={400: {"model": ErrorResponse}, 403: {"model": ErrorResponse},
                   413: {"model": ErrorResponse}, 415: {"model": ErrorResponse},
                   500: {"model": ErrorResponse}, 504: {"model": ErrorResponse}},
    )
    async def render(
        request: Request,
        output_format: str = Query("jpg"),
        width: str = Query("0"),
        height: str = Query("0"),
        quality: str = Query("80"),
        hq: str = Query("false"),
        upscale: str = Query("false"),
        apply_custom_presets: str = Query("false"),
        dt_arg: list[str] = Query(default=[]),
        dt_conf: list[str] = Query(default=[]),
    ) -> FileResponse:
        # --- security level gating ---
        if not settings.is_render_allowed():
            raise HTTPException(
                status_code=403,
                detail="Render endpoint is disabled at the current SECURITY_LEVEL",
            )
        if not settings.is_render_passthrough_allowed() and (dt_arg or dt_conf):
            raise HTTPException(
                status_code=403,
                detail="Advanced darktable passthrough arguments are not allowed "
                       "at the current SECURITY_LEVEL",
            )

        filename, ext = _validate_filename_header(request, settings)

        # --- validate parameters ---
        allowed_formats = settings.allowed_output_formats_set()
        try:
            fmt = validate_output_format(output_format, allowed_formats)
            w = validate_int("width", width, 0, 16000)
            h = validate_int("height", height, 0, 16000)
            q = validate_int("quality", quality, 1, 100)
            hq_val = validate_bool("hq", hq)
            up_val = validate_bool("upscale", upscale)
            acp_val = validate_bool("apply_custom_presets", apply_custom_presets)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        safe_args: list[str] = []
        safe_confs: list[str] = []
        try:
            for a in dt_arg:
                safe_args.append(validate_dt_arg(a))
            for c in dt_conf:
                safe_confs.append(validate_dt_conf(c))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        params = RenderParams(
            width=w if w > 0 else None,
            height=h if h > 0 else None,
            hq=hq_val,
            upscale=up_val,
            apply_custom_presets=acp_val,
            output_format=fmt,
            quality=q,
            extra_args=safe_args,
            extra_confs=safe_confs,
        )

        return await _render_pipeline(
            request=request, filename=filename, ext=ext,
            params=params, settings=settings, endpoint="render",
        )

    @application.post(
        "/preview",
        response_model=None,
        summary="Fixed preset preview (raw binary body)",
        description=(
            "Server-to-server preview endpoint optimised for integrations "
            "like Nextcloud. Send the RAW file as the raw HTTP request body "
            "with Content-Type: application/octet-stream and an X-Filename "
            "header. All rendering settings are taken from server "
            "configuration only."
        ),
        responses={400: {"model": ErrorResponse}, 413: {"model": ErrorResponse},
                   415: {"model": ErrorResponse}, 500: {"model": ErrorResponse},
                   504: {"model": ErrorResponse}},
    )
    async def preview(request: Request) -> FileResponse:
        filename, ext = _validate_filename_header(request, settings)

        params = RenderParams(
            width=settings.preview_width if settings.preview_width > 0 else None,
            height=settings.preview_height if settings.preview_height > 0 else None,
            hq=settings.preview_hq,
            upscale=settings.preview_upscale,
            apply_custom_presets=settings.preview_apply_custom_presets,
            output_format=settings.preview_format,
            quality=settings.preview_quality,
        )

        return await _render_pipeline(
            request=request, filename=filename, ext=ext,
            params=params, settings=settings, endpoint="preview",
        )

    return application


app = create_app()
