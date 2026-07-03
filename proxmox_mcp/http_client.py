"""Async HTTP helpers for the Proxmox REST API."""
from __future__ import annotations

import asyncio
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


async def wait_for_task(node: str, upid: Any, wait_seconds: int) -> str:
    """Poll a PVE task's status for up to `wait_seconds` and return a short
    suffix describing the outcome (empty string when not waiting or when the
    task id isn't a UPID). Saves the caller a follow-up status round trip."""
    if wait_seconds <= 0 or not isinstance(upid, str) or not upid.startswith("UPID:"):
        return ""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + wait_seconds
    delay = 1.0
    while True:
        try:
            st = await get(f"/nodes/{node}/tasks/{upid}/status")
        except Exception:
            return " (task status poll failed — check proxmox_get_task_log)"
        if st and st.get("status") == "stopped":
            exitstatus = st.get("exitstatus", "?")
            if exitstatus == "OK":
                return " Task finished: OK."
            return f" Task FAILED: {exitstatus}. See proxmox_get_task_log for details."
        remaining = deadline - loop.time()
        if remaining <= 0:
            return (
                f" Task still running after {wait_seconds}s — it continues in "
                "the background; check later with proxmox_get_task_log."
            )
        await asyncio.sleep(min(delay, remaining))
        delay = min(delay * 1.5, 5.0)
