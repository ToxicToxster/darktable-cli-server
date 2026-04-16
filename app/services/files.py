"""File handling: temp directory management and upload writing."""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import UploadFile

logger = logging.getLogger("darktable_server.files")


def create_temp_dir(base: str) -> Path:
    """Create a temporary working directory under *base*."""
    Path(base).mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(dir=base, prefix="render-"))


def cleanup_temp_dir(path: Path) -> None:
    """Remove a temporary directory tree. Safe to call multiple times."""
    try:
        shutil.rmtree(path, ignore_errors=True)
        logger.debug("Cleaned up temp dir: %s", path)
    except Exception:
        logger.exception("Failed to clean up temp dir: %s", path)


async def write_upload(upload: UploadFile, dest: Path) -> int:
    """Stream an uploaded file to *dest*. Returns bytes written."""
    total = 0
    with dest.open("wb") as f:
        while True:
            chunk = await upload.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
    return total


async def write_body_to_file(receive, dest: Path, max_bytes: int) -> int:
    """Stream raw request body to *dest*. Returns bytes written.

    Raises *ValueError* if the body exceeds *max_bytes*.
    """
    total = 0
    with dest.open("wb") as f:
        async for chunk in receive:
            f.write(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"Body exceeds maximum size of {max_bytes} bytes")
    return total
