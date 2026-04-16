"""Pydantic models for request validation and response schemas."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    error: str
    details: Optional[Any] = None


class HealthResponse(BaseModel):
    status: str = "ok"


class VersionResponse(BaseModel):
    app_version: str
    python_version: str
    darktable_cli_available: bool
    darktable_version: Optional[str] = None
