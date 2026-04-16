"""Wrapper around darktable-cli: builds argv, runs subprocess, returns result."""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("darktable_server.darktable")

# Map logical format names to file extensions
FORMAT_EXTENSION_MAP: dict[str, str] = {
    "jpg": ".jpg",
    "jpeg": ".jpg",
    "png": ".png",
    "tif": ".tiff",
    "tiff": ".tiff",
}

# Map format to the darktable core-conf key prefix
FORMAT_CONF_PREFIX: dict[str, str] = {
    "jpg": "plugins/imageio/format/jpeg",
    "jpeg": "plugins/imageio/format/jpeg",
    "png": "plugins/imageio/format/png",
    "tif": "plugins/imageio/format/tiff",
    "tiff": "plugins/imageio/format/tiff",
}


@dataclass
class RenderParams:
    """Structured parameters for a darktable-cli invocation."""

    width: Optional[int] = None
    height: Optional[int] = None
    hq: bool = False
    upscale: bool = False
    apply_custom_presets: bool = False
    output_format: str = "jpg"
    quality: int = 80
    extra_args: list[str] = field(default_factory=list)
    extra_confs: list[str] = field(default_factory=list)


@dataclass
class RenderResult:
    success: bool
    output_path: Optional[Path] = None
    returncode: int = -1
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    error: Optional[str] = None


def get_darktable_cli_path() -> Optional[str]:
    return shutil.which("darktable-cli")


def build_command(
    cli_path: str,
    input_path: Path,
    output_path: Path,
    params: RenderParams,
) -> list[str]:
    """Build darktable-cli argv list. Never uses shell strings."""
    cmd: list[str] = [cli_path, str(input_path), str(output_path)]

    if params.width is not None:
        cmd += ["--width", str(params.width)]
    if params.height is not None:
        cmd += ["--height", str(params.height)]

    cmd += ["--hq", "1" if params.hq else "0"]
    cmd += ["--upscale", "1" if params.upscale else "0"]
    cmd += ["--apply-custom-presets", "true" if params.apply_custom_presets else "false"]

    # Extra validated argv tokens
    for arg in params.extra_args:
        cmd.append(arg)

    # Core conf block
    core_confs: list[str] = []

    # Quality for JPEG
    fmt = params.output_format.lower()
    if fmt in ("jpg", "jpeg"):
        core_confs.append(f"plugins/imageio/format/jpeg/quality={params.quality}")

    # Extra validated --conf entries
    for conf in params.extra_confs:
        core_confs.append(conf)

    if core_confs:
        cmd.append("--core")
        for c in core_confs:
            cmd += ["--conf", c]

    return cmd


def run_render(
    input_path: Path,
    output_path: Path,
    params: RenderParams,
    timeout: int,
) -> RenderResult:
    """Execute darktable-cli and return a structured result."""
    cli_path = get_darktable_cli_path()
    if cli_path is None:
        return RenderResult(success=False, error="darktable-cli is not installed or not in PATH")

    cmd = build_command(cli_path, input_path, output_path, params)

    logger.info(
        "Rendering: format=%s width=%s height=%s hq=%s",
        params.output_format, params.width, params.height, params.hq,
    )
    logger.debug("Command: %s", cmd)

    t0 = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - t0
        logger.error("darktable-cli timed out after %.1fs", duration)
        return RenderResult(
            success=False,
            returncode=-1,
            stderr=str(exc.stderr or ""),
            duration_seconds=duration,
            error=f"Render timed out after {timeout}s",
        )
    except FileNotFoundError:
        return RenderResult(success=False, error="darktable-cli binary not found at runtime")

    duration = time.monotonic() - t0

    logger.info("darktable-cli finished: exit=%d duration=%.1fs", result.returncode, duration)
    if result.stdout:
        logger.debug("stdout: %s", result.stdout.strip()[:500])
    if result.stderr:
        logger.debug("stderr: %s", result.stderr.strip()[:500])

    if result.returncode != 0:
        return RenderResult(
            success=False,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=duration,
            error="darktable-cli returned non-zero exit code",
        )

    if not output_path.exists() or output_path.stat().st_size == 0:
        return RenderResult(
            success=False,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=duration,
            error="darktable-cli produced no output file",
        )

    return RenderResult(
        success=True,
        output_path=output_path,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_seconds=duration,
    )
