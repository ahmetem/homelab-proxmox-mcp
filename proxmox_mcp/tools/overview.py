"""Single-call health overview.

Answers the everyday "how is the server / is anything wrong / any space
left?" question in ONE tool call instead of four to six (node status +
guest list + storage + ZFS). Output is intentionally compact.
"""
from __future__ import annotations

import asyncio

from pydantic import BaseModel, ConfigDict, Field

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import fmt_bytes, fmt_uptime, health_icon
from proxmox_mcp.mcp_instance import mcp


class HealthOverviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(default="pve", description="Node to summarize.")


@mcp.tool(
    name="proxmox_health_overview",
    annotations={
        "title": "Health Overview (node + guests + storage + ZFS)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_health_overview(
    params: HealthOverviewInput = HealthOverviewInput(),
) -> str:
    """Compact one-call summary: node load, guest states, storage fill
    levels, ZFS pool health. Use this FIRST for general "how is the
    server" questions; drill down with the specific tools only if
    something looks wrong."""
    cfg = require_config()
    if cfg:
        return cfg

    node = params.node
    try:
        status, guests, storages, pools = await asyncio.gather(
            http_client.get(f"/nodes/{node}/status"),
            http_client.get("/cluster/resources", params={"type": "vm"}),
            http_client.get(f"/nodes/{node}/storage"),
            http_client.get(f"/nodes/{node}/disks/zfs"),
            return_exceptions=True,
        )
    except Exception as exc:  # pragma: no cover - gather itself rarely raises
        return http_client.format_http_error(exc)

    lines = [f"## Health overview — `{node}`", ""]

    # Node
    if isinstance(status, Exception):
        lines.append(f"- **Node**: {http_client.format_http_error(status)}")
    else:
        mem = status.get("memory", {}) or {}
        rootfs = status.get("rootfs", {}) or {}
        load = status.get("loadavg", []) or []
        lines.append(
            f"- **Node**: uptime {fmt_uptime(status.get('uptime', 0))}, "
            f"CPU {(status.get('cpu') or 0) * 100:.0f}%, "
            f"Mem {fmt_bytes(mem.get('used'))}/{fmt_bytes(mem.get('total'))}, "
            f"RootFS {fmt_bytes(rootfs.get('used'))}/{fmt_bytes(rootfs.get('total'))}"
            + (f", load {', '.join(str(x) for x in load)}" if load else "")
        )

    # Guests
    if isinstance(guests, Exception):
        lines.append(f"- **Guests**: {http_client.format_http_error(guests)}")
    else:
        guests = [g for g in (guests or []) if g.get("node") == node] or (guests or [])
        running = [g for g in guests if g.get("status") == "running"]
        stopped = [g for g in guests if g.get("status") != "running"]
        stopped_str = ""
        if stopped:
            names = ", ".join(
                f"{g.get('vmid')} `{g.get('name', '?')}`"
                for g in sorted(stopped, key=lambda x: x.get("vmid", 0))
            )
            stopped_str = f" — stopped: {names}"
        lines.append(
            f"- **Guests**: {len(running)} running, {len(stopped)} stopped"
            f"{stopped_str}"
        )

    # Storage
    if isinstance(storages, Exception):
        lines.append(f"- **Storage**: {http_client.format_http_error(storages)}")
    else:
        parts = []
        for s in sorted(storages or [], key=lambda x: str(x.get("storage", ""))):
            used = s.get("used", 0) or 0
            total = s.get("total", 0) or 0
            pct = (used / total * 100) if total else 0
            warn = " ⚠" if pct >= 85 else ""
            active = "" if s.get("active") else " (inactive)"
            parts.append(f"{s.get('storage')} {pct:.0f}%{warn}{active}")
        lines.append("- **Storage**: " + (" | ".join(parts) or "none"))

    # ZFS pools
    if isinstance(pools, Exception):
        lines.append(f"- **ZFS**: {http_client.format_http_error(pools)}")
    else:
        parts = []
        for p in pools or []:
            health = (p.get("health") or "?").upper()
            parts.append(
                f"{health_icon(health)} {p.get('name')} {health} "
                f"({fmt_bytes(p.get('alloc', 0))}/{fmt_bytes(p.get('size', 0))})"
            )
        lines.append("- **ZFS**: " + (" | ".join(parts) or "no pools"))

    return "\n".join(lines)
