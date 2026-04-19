from __future__ import annotations

import httpx

from ..config import get_settings


def client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        headers={"User-Agent": get_settings().http_user_agent, "Accept": "application/json"},
    )
