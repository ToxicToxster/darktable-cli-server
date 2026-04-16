"""FastAPI dependencies: render semaphore."""

from __future__ import annotations

import asyncio
from typing import Optional

_semaphore: Optional[asyncio.Semaphore] = None
_max_concurrent: int = 4


def init_semaphore(max_concurrent: int) -> None:
    global _semaphore, _max_concurrent
    _max_concurrent = max_concurrent
    _semaphore = asyncio.Semaphore(max_concurrent)


def get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_max_concurrent)
    return _semaphore
