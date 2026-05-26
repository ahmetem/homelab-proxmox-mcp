"""ZFS pool inventory (read-only).

Phase 1 tools:
  - GET /nodes/{node}/disks/zfs           -> list pools
  - GET /nodes/{node}/disks/zfs/{name}    -> single pool detail (vdev tree, errors)

Phase 2 will add zpool create/destroy; Phase 3 (with SSH) will add dataset,
snapshot, send/recv, and property tools.
"""
from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import fmt_bytes, health_icon
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import NodeInput, ResponseFormat


class ZfsPoolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., description="Node name", min_length=1)
    name: str = Field(
        ...,
        description="ZFS pool name (e.g., 'vmdata', 'rpool')",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'.",
    )


def _render_vdev_tree(node: dict, depth: int = 0) -> list[str]:
    """Recursively render the vdev tree returned by /disks/zfs/{name}."""
    out: list[str] = []
    indent = "  " * depth
    name = node.get("name", "?")
    state = (node.get("state") or "").upper()
    icon = health_icon(state) if state else ""
    read = node.get("read", 0)
    write = node.get("write", 0)
    cksum = node.get("cksum", 0)
    errors = ""
    if read or write or cksum:
        errors = f"  _(R:{read} W:{write} CK:{cksum})_"
    line = f"{indent}- {icon} `{name}`"
    if state:
        line += f" — {state}"
    line += errors
    msg = node.get("msg")
    if msg:
        line += f" — {msg}"
    out.append(line)
    for child in node.get("children", []) or []:
        out.extend(_render_vdev_tree(child, depth + 1))
    return out


@mcp.tool(
    name="proxmox_list_zfs",
    annotations={
        "title": "List ZFS Pools",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_list_zfs(params: NodeInput) -> str:
    """List ZFS pools on a node with size, free space, dedup ratio, and health.

    For per-pool vdev breakdown and error counters, use proxmox_get_zfs_pool.

    Returns:
        str: Markdown table or JSON list of pools.
    """
    cfg = require_config()
    if cfg:
        return cfg

    try:
        pools = await http_client.get(f"/nodes/{params.node}/disks/zfs")
    except Exception as exc:
        return http_client.format_http_error(exc)

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(pools, indent=2, default=str)

    if not pools:
        return f"_No ZFS pools on `{params.node}`._"

    lines = [
        f"## ZFS pools on `{params.node}`",
        "",
        "| Pool | Health | Size | Allocated | Free | Frag | Dedup |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for p in pools:
        name = p.get("name", "?")
        health = (p.get("health") or "?").upper()
        icon = health_icon(health)
        size = fmt_bytes(p.get("size", 0))
        alloc = fmt_bytes(p.get("alloc", 0))
        free = fmt_bytes(p.get("free", 0))
        frag = p.get("frag")
        frag_str = f"{frag}%" if isinstance(frag, (int, float)) else (str(frag) if frag else "?")
        dedup = p.get("dedup")
        dedup_str = f"{dedup:.2f}x" if isinstance(dedup, (int, float)) else (str(dedup) if dedup else "?")
        lines.append(
            f"| `{name}` | {icon} {health} | {size} | {alloc} | {free} | "
            f"{frag_str} | {dedup_str} |"
        )
    return "\n".join(lines)


@mcp.tool(
    name="proxmox_get_zfs_pool",
    annotations={
        "title": "Get ZFS Pool Detail (vdevs, errors)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_get_zfs_pool(params: ZfsPoolInput) -> str:
    """Show vdev tree and per-device error counters for a single ZFS pool.

    Read/Write/Checksum (R/W/CK) error counts are shown next to each device.
    Any non-ONLINE state is flagged.

    Returns:
        str: Markdown tree of vdevs, or JSON.
    """
    cfg = require_config()
    if cfg:
        return cfg

    try:
        data = await http_client.get(
            f"/nodes/{params.node}/disks/zfs/{params.name}"
        )
    except Exception as exc:
        return http_client.format_http_error(exc)

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2, default=str)

    if not data:
        return f"_Pool `{params.name}` not found on `{params.node}`._"

    lines = [f"## ZFS pool `{params.name}` on `{params.node}`", ""]

    state = (data.get("state") or "?").upper()
    scan = data.get("scan")
    errors = data.get("errors")
    lines.append(f"- **State**: {health_icon(state)} {state}")
    if scan:
        lines.append(f"- **Scan**: {scan}")
    if errors:
        lines.append(f"- **Errors**: {errors}")
    lines.append("")
    lines.append("### vdev tree")
    children = data.get("children") or []
    if not children:
        lines.append("_No vdev tree returned by API._")
    for c in children:
        lines.extend(_render_vdev_tree(c, depth=0))

    return "\n".join(lines)
