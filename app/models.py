"""Pydantic models and shared API payload helpers."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    error: str
    details: Optional[Any] = None


def build_error_payload(error: str, details: Optional[Any] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": error}
    if details is not None:
        payload["details"] = details
    return payload


def normalize_error_payload(detail: Any, default_error: str = "Request failed") -> dict[str, Any]:
    if isinstance(detail, str):
        return build_error_payload(detail)

    if isinstance(detail, dict):
        if "error" in detail:
            error = str(detail["error"])
            details = detail.get("details")
            extra = {key: value for key, value in detail.items() if key not in {"error", "details"}}
            if details is None:
                combined = extra or None
            elif extra and isinstance(details, dict):
                combined = {**details, **extra}
            elif extra:
                combined = {"detail": details, **extra}
            else:
                combined = details
            return build_error_payload(error, combined)
        return build_error_payload(default_error, detail)

    if detail is None:
        return build_error_payload(default_error)

    return build_error_payload(default_error, detail)


class HealthResponse(BaseModel):
    status: str = "ok"


class VersionResponse(BaseModel):
    app_version: str
    python_version: str
    darktable_cli_available: bool
    darktable_version: Optional[str] = None
