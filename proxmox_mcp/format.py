"""Formatting helpers shared across modules."""
from __future__ import annotations

from typing import Any


def fmt_bytes(n: Any) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    if n < 1024:
        return f"{n:.0f} B"
    for unit in ["KB", "MB", "GB", "TB"]:
        n /= 1024
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PB"


def fmt_uptime(secs: Any) -> str:
    try:
        s = int(secs)
    except (TypeError, ValueError):
        return "?"
    if s <= 0:
        return "-"
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m or not parts:
        parts.append(f"{m}m")
    return " ".join(parts)


def status_icon(status: str) -> str:
    return {
        "running": "\U0001F7E2",
        "online": "\U0001F7E2",
        "stopped": "\U0001F534",
        "offline": "\U0001F534",
    }.get(status, "\u26AA")


def health_icon(health: str) -> str:
    """ZFS / SMART health icon."""
    h = (health or "").upper()
    if h in {"ONLINE", "PASSED", "OK"}:
        return "\U0001F7E2"
    if h in {"DEGRADED", "WARNING"}:
        return "\U0001F7E1"
    if h in {"FAULTED", "FAILED", "OFFLINE", "UNAVAIL"}:
        return "\U0001F534"
    return "\u26AA"


def missing_confirm(action: str) -> str:
    return (
        f"Refused: '{action}' requires confirm=true. "
        "Ask the user to confirm, then retry with confirm=true."
    )


def missing_data_loss_ack(action: str) -> str:
    return (
        f"Refused: '{action}' is destructive and requires "
        "i_understand_data_loss=true in addition to confirm=true. "
        "Explain the consequences to the user and ask explicitly."
    )
