"""Physical disk inventory and SMART (read-only).

Phase 1 tools: lists all block devices (HDD, SSD, NVMe) on a node and exposes
SMART health data. These power the "what disks does the host actually see?"
question — including disks that Proxmox storage pools don't reference yet.

Backed by Proxmox REST endpoints:
  - GET /nodes/{node}/disks/list
  - GET /nodes/{node}/disks/smart?disk=/dev/X
"""
from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import fmt_bytes, health_icon
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import NodeInput, ResponseFormat


class DiskSmartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., description="Node name (e.g., 'pve')", min_length=1)
    disk: str = Field(
        ...,
        description=(
            "Block device path as reported by proxmox_list_disks "
            "(e.g. '/dev/sda', '/dev/nvme0n1')."
        ),
        min_length=1,
        max_length=64,
        pattern=r"^/dev/[A-Za-z0-9/_-]+$",
    )
    healthonly: bool = Field(
        default=False,
        description=(
            "If true, request only the overall health verdict (faster, less data). "
            "If false, return full attribute table."
        ),
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'.",
    )


class DiskListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., description="Node name (e.g., 'pve')", min_length=1)
    include_partitions: bool = Field(
        default=False,
        description=(
            "If true, include partitions/children of each disk. "
            "If false (default), only top-level block devices."
        ),
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'.",
    )


def _disk_type_label(d: dict) -> str:
    t = (d.get("type") or "").lower()
    if t == "nvme":
        return "NVMe"
    if t == "ssd":
        return "SSD"
    if t == "hdd":
        return "HDD"
    if t == "usb":
        return "USB"
    return t.upper() or "?"


def _used_label(d: dict) -> str:
    """Translate the 'used' field reported by Proxmox into a friendly label."""
    u = d.get("used")
    if not u:
        return "free"
    u = str(u).lower()
    return {
        "lvm": "LVM",
        "zfs": "ZFS",
        "ceph": "Ceph",
        "partitions": "partitioned",
        "mounted": "mounted",
        "bios-boot": "BIOS boot",
    }.get(u, u)


@mcp.tool(
    name="proxmox_list_disks",
    annotations={
        "title": "List Physical Disks (host)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_list_disks(params: DiskListInput) -> str:
    """List all physical block devices on a node (HDD, SSD, NVMe).

    Reports vendor, model, serial, size, type, and how the disk is currently
    used (free / LVM / ZFS / partitioned / mounted). NVMe devices are included.

    Use this to answer 'what disks does the host actually see?' — including
    disks that aren't yet exposed as a Proxmox storage pool.

    Returns:
        str: Markdown table or JSON list of disks.
    """
    cfg = require_config()
    if cfg:
        return cfg

    query = {}
    if params.include_partitions:
        query["include-partitions"] = 1

    try:
        disks = await http_client.get(
            f"/nodes/{params.node}/disks/list",
            params=query or None,
        )
    except Exception as exc:
        return http_client.format_http_error(exc)

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(disks, indent=2, default=str)

    if not disks:
        return f"_No disks reported on `{params.node}`._"

    lines = [
        f"## Disks on `{params.node}`",
        "",
        "| Device | Type | Size | Model | Serial | Used | Health |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for d in sorted(disks, key=lambda x: str(x.get("devpath", ""))):
        device = d.get("devpath", "?")
        dtype = _disk_type_label(d)
        size = fmt_bytes(d.get("size"))
        model = (d.get("model") or "?").strip()
        serial = (d.get("serial") or "?").strip()
        used = _used_label(d)
        health_raw = d.get("health") or d.get("wearout")
        if isinstance(health_raw, (int, float)) and 0 <= health_raw <= 100:
            health_str = f"wearout {health_raw}%"
            icon = health_icon("PASSED") if health_raw < 80 else health_icon("WARNING")
        else:
            health_str = str(health_raw or "?")
            icon = health_icon(health_str)
        lines.append(
            f"| `{device}` | {dtype} | {size} | {model} | {serial} | {used} | "
            f"{icon} {health_str} |"
        )
    return "\n".join(lines)


@mcp.tool(
    name="proxmox_get_disk_smart",
    annotations={
        "title": "Get Disk SMART Health",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_get_disk_smart(params: DiskSmartInput) -> str:
    """Fetch SMART data for a single disk.

    Set healthonly=true for just the overall PASSED/FAILED verdict (fast).
    Default (healthonly=false) returns the full attribute table.

    For NVMe devices Proxmox returns NVMe-style fields (critical warning,
    media errors, percentage used, temperature, etc.) instead of classic
    SMART attributes — the tool renders both formats automatically.

    Returns:
        str: Markdown summary (and attributes if available) or JSON.
    """
    cfg = require_config()
    if cfg:
        return cfg

    query: dict = {"disk": params.disk}
    if params.healthonly:
        query["healthonly"] = 1

    try:
        data = await http_client.get(
            f"/nodes/{params.node}/disks/smart", params=query
        )
    except Exception as exc:
        return http_client.format_http_error(exc)

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2, default=str)

    if not data:
        return f"_No SMART data available for `{params.disk}`._"

    health = (data.get("health") or "?").upper()
    icon = health_icon(health)
    lines = [
        f"## SMART for `{params.disk}` on `{params.node}`",
        "",
        f"- **Overall health**: {icon} {health}",
    ]

    smart_type = data.get("type")
    if smart_type:
        lines.append(f"- **Report type**: {smart_type}")

    attrs = data.get("attributes")
    text_blob = data.get("text")

    if isinstance(attrs, list) and attrs:
        lines.append("")
        lines.append("| ID | Attribute | Value | Worst | Threshold | Raw | Status |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for a in attrs:
            lines.append(
                f"| {a.get('id', '?')} | {a.get('name', '?')} | "
                f"{a.get('value', '?')} | {a.get('worst', '?')} | "
                f"{a.get('threshold', '?')} | {a.get('raw', '?')} | "
                f"{a.get('flags', '?')} |"
            )
    elif text_blob:
        lines.append("")
        lines.append("```")
        lines.append(str(text_blob).strip())
        lines.append("```")

    return "\n".join(lines)
