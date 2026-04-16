"""darktable-cli-server: production-grade HTTP wrapper around darktable-cli."""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from app.config import Settings, get_app_version, get_darktable_version, get_settings
from app.deps import get_semaphore, init_semaphore
from app.models import ErrorResponse, HealthResponse, VersionResponse
from app.security import (
    APIKeyMiddleware,
    MaxUploadSizeMiddleware,
    derive_output_filename,
    validate_bool,
    validate_dt_arg,
    validate_dt_conf,
    validate_int,
    validate_output_format,
)
from app.services.darktable import FORMAT_EXTENSION_MAP, RenderParams, get_darktable_cli_path, run_render
from app.services.files import cleanup_temp_dir, create_temp_dir, write_upload

logger = logging.getLogger("darktable_server")


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
        init_semaphore(settings.max_concurrent_renders)
        Path(settings.temp_dir).mkdir(parents=True, exist_ok=True)
        cli = get_darktable_cli_path()
        if cli:
            logger.info("darktable-cli found: %s", cli)
        else:
            logger.warning("darktable-cli NOT found — /render and /preview will fail")
        yield

    application = FastAPI(
        title="darktable-cli-server",
        version=get_app_version(),
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    application.add_middleware(MaxUploadSizeMiddleware, max_bytes=settings.max_upload_bytes)
    application.add_middleware(APIKeyMiddleware, settings=settings)

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
        responses={400: {"model": ErrorResponse}, 413: {"model": ErrorResponse},
                   415: {"model": ErrorResponse}, 500: {"model": ErrorResponse},
                   504: {"model": ErrorResponse}},
    )
    async def render(
        request: Request,
        file: UploadFile = File(...),
        output_format: str = Form("jpg"),
        width: str = Form("0"),
        height: str = Form("0"),
        quality: str = Form("80"),
        hq: str = Form("0"),
        upscale: str = Form("0"),
        apply_custom_presets: str = Form("false"),
    ) -> FileResponse | JSONResponse:
        form = await request.form()
        dt_args: list[str] = [
            v for k, v in form.multi_items() if k == "dt_arg" and isinstance(v, str)
        ]
        dt_confs: list[str] = [
            v for k, v in form.multi_items() if k == "dt_conf" and isinstance(v, str)
        ]
        return await _do_render(
            file=file, output_format=output_format, width=width, height=height,
            quality=quality, hq=hq, upscale=upscale,
            apply_custom_presets=apply_custom_presets,
            dt_args=dt_args, dt_confs=dt_confs, settings=settings, endpoint="render",
        )

    @application.post(
        "/preview",
        response_model=None,
        responses={400: {"model": ErrorResponse}, 413: {"model": ErrorResponse},
                   415: {"model": ErrorResponse}, 500: {"model": ErrorResponse},
                   504: {"model": ErrorResponse}},
    )
    async def preview(
        file: UploadFile = File(...),
    ) -> FileResponse | JSONResponse:
        return await _do_render(
            file=file,
            output_format=settings.preview_format,
            width=str(settings.preview_width),
            height=str(settings.preview_height),
            quality=str(settings.preview_quality),
            hq=str(settings.preview_hq).lower(),
            upscale=str(settings.preview_upscale).lower(),
            apply_custom_presets=str(settings.preview_apply_custom_presets).lower(),
            dt_args=[], dt_confs=[], settings=settings, endpoint="preview",
        )

    return application


def _error(status: int, msg: str, details: object = None) -> JSONResponse:
    payload: dict = {"error": msg}
    if details is not None:
        payload["details"] = details
    return JSONResponse(status_code=status, content=payload)


async def _do_render(
    *,
    file: UploadFile,
    output_format: str,
    width: str,
    height: str,
    quality: str,
    hq: str,
    upscale: str,
    apply_custom_presets: str,
    dt_args: list[str],
    dt_confs: list[str],
    settings: Settings,
    endpoint: str,
) -> FileResponse | JSONResponse:
    allowed_formats = settings.allowed_output_formats_set()
    allowed_exts = settings.allowed_raw_extensions_set()
    denylist = settings.dt_arg_denylist_set()

    try:
        fmt = validate_output_format(output_format, allowed_formats)
        w = validate_int("width", width, 0, 16000)
        h = validate_int("height", height, 0, 16000)
        q = validate_int("quality", quality, 1, 100)
        hq_val = validate_bool("hq", hq)
        up_val = validate_bool("upscale", upscale)
        acp_val = validate_bool("apply_custom_presets", apply_custom_presets)
    except ValueError as exc:
        return _error(400, "Invalid parameters", str(exc))

    safe_args: list[str] = []
    safe_confs: list[str] = []
    try:
        for a in dt_args:
            safe_args.append(validate_dt_arg(a, denylist))
        for c in dt_confs:
            safe_confs.append(validate_dt_conf(c))
    except ValueError as exc:
        return _error(400, "Invalid advanced parameter", str(exc))

    if not file.filename:
        return _error(400, "No file uploaded")
    ext = Path(file.filename).suffix.lower()
    if ext not in allowed_exts:
        return _error(415, "Unsupported file type", {
            "extension": ext, "allowed": sorted(allowed_exts),
        })

    temp_dir = create_temp_dir(settings.temp_dir)
    cleanup_needed = True

    try:
        out_ext = FORMAT_EXTENSION_MAP.get(fmt, f".{fmt}")
        input_path = temp_dir / f"input{ext}"
        output_path = temp_dir / f"output{out_ext}"

        size = await write_upload(file, input_path)
        await file.close()

        if size == 0:
            return _error(400, "Uploaded file is empty")
        if size > settings.max_upload_bytes:
            return _error(413, "Upload exceeds maximum size")

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

        sem = get_semaphore()
        try:
            async with asyncio.timeout(settings.request_timeout + 5):
                await sem.acquire()
        except TimeoutError:
            return _error(504, "Server too busy, render queue full")

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, run_render, input_path, output_path, params, settings.request_timeout,
            )
        finally:
            sem.release()

        if not result.success:
            if result.error and "timed out" in result.error.lower():
                return _error(504, "Render timeout", {"seconds": settings.request_timeout})
            return _error(500, "Render failed", {
                "message": result.error,
                "returncode": result.returncode,
                "stderr": (result.stderr or "")[:1000],
            })

        response_filename = derive_output_filename(file.filename, out_ext.lstrip("."))
        media = "image/jpeg" if fmt in ("jpg", "jpeg") else f"image/{out_ext.lstrip('.')}"
        if fmt in ("tif", "tiff"):
            media = "image/tiff"
        if fmt == "png":
            media = "image/png"

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


app = create_app()
