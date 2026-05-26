"""Async HTTP helpers for the Proxmox REST API."""
from __future__ import annotations

from typing import Any, Optional

import httpx

from proxmox_mcp import config


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=config.base_url(),
        headers=config.auth_header(),
        verify=config.PROXMOX_VERIFY_SSL,
        timeout=config.PROXMOX_TIMEOUT,
    )


def format_http_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        body = exc.response.text[:300]
        if status == 401:
            return "Error: Authentication failed. Check PROXMOX_TOKEN_VALUE."
        if status == 403:
            return f"Error: Permission denied. Token lacks privileges. {body}"
        if status == 404:
            return f"Error: Resource not found. {body}"
        return f"Error: HTTP {status}: {body}"
    if isinstance(exc, httpx.ConnectError):
        return f"Error: Cannot connect to {config.PROXMOX_HOST}:{config.PROXMOX_PORT}"
    if isinstance(exc, httpx.TimeoutException):
        return f"Error: Request timed out after {config.PROXMOX_TIMEOUT}s"
    return f"Error: {type(exc).__name__}: {exc}"


async def get(path: str, params: Optional[dict] = None) -> Any:
    async with client() as c:
        r = await c.get(path, params=params)
        r.raise_for_status()
        return r.json().get("data")


async def post(path: str, data: Optional[dict] = None) -> Any:
    async with client() as c:
        r = await c.post(path, data=data or {})
        r.raise_for_status()
        return r.json().get("data")


async def put(path: str, data: Optional[dict] = None) -> Any:
    async with client() as c:
        r = await c.put(path, data=data or {})
        r.raise_for_status()
        return r.json().get("data")


async def delete(path: str, params: Optional[dict] = None) -> Any:
    async with client() as c:
        r = await c.delete(path, params=params)
        r.raise_for_status()
        return r.json().get("data")
