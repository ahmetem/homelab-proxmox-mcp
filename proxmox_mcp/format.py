"""Formatting helpers shared across modules."""
from __future__ import annotations

import json
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


def truncate(s: str, limit: int = 8000) -> str:
    """Hard-cap a response string so a pathological result can't blow up the
    model's context. Appends a marker noting the original length."""
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n\n... [truncated, total {len(s)} chars]"


def project_fields(obj: Any, fields: Any) -> Any:
    """Keep only `fields` keys in a dict (or in each dict of a list). Non-dict
    values and empty/None `fields` pass through unchanged. Unknown field names
    simply match nothing, so a typo yields empty objects rather than an error."""
    if not fields:
        return obj
    if isinstance(obj, list):
        return [project_fields(o, fields) for o in obj]
    if isinstance(obj, dict):
        return {k: v for k, v in obj.items() if k in fields}
    return obj


def compact_json(obj: Any, limit: int = 8000, fields: Any = None) -> str:
    """Serialize `obj` to compact JSON (no indent whitespace) and hard-cap the
    length. Used by the opt-in `response_format=json` branches.

    Compact separators drop ~30-40% of the bytes vs `indent=2`. `fields`
    optionally projects dicts (or list items) down to the given keys first.

    Over-limit lists are truncated at the *item* level so the output stays
    valid JSON: items are dropped from the end and a final
    `{"_truncated_items": N}` marker records how many were cut. Only a
    non-list payload that alone exceeds the limit falls back to a hard
    (invalid-JSON) string cap."""
    obj = project_fields(obj, fields)

    def dumps(o: Any) -> str:
        return json.dumps(o, separators=(",", ":"), default=str)

    s = dumps(obj)
    if len(s) <= limit:
        return s

    if isinstance(obj, list) and len(obj) > 1:
        # First guess proportionally, then halve until it fits.
        keep = max(1, int(len(obj) * limit / len(s)))
        while keep >= 1:
            out = dumps(obj[:keep] + [{"_truncated_items": len(obj) - keep}])
            if len(out) <= limit:
                return out
            if keep == 1:
                break
            keep //= 2

    return truncate(s, limit)
