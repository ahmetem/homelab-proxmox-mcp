"""LVM and LVM-thin inventory (read-only).

Phase 1 tools — listing only:
  - GET /nodes/{node}/disks/lvm        -> VGs + PVs + LVs
  - GET /nodes/{node}/disks/lvmthin    -> thin pools

Phase 2 will add create/destroy actions.
"""
from __future__ import annotations

import json

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import fmt_bytes
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import NodeInput, ResponseFormat


def _pct(used, total) -> str:
    try:
        used_f = float(used)
        total_f = float(total)
    except (TypeError, ValueError):
        return "?"
    if total_f <= 0:
        return "?"
    return f"{(used_f / total_f) * 100:.0f}%"


@mcp.tool(
    name="proxmox_list_lvm",
    annotations={
        "title": "List LVM (PVs, VGs, LVs)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_list_lvm(params: NodeInput) -> str:
    """List LVM volume groups on a node, with their physical volumes and logical volumes.

    Returns:
        str: For each VG: size/free, member PVs, contained LVs.
    """
    cfg = require_config()
    if cfg:
        return cfg

    try:
        data = await http_client.get(f"/nodes/{params.node}/disks/lvm")
    except Exception as exc:
        return http_client.format_http_error(exc)

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2, default=str)

    children = data.get("children") if isinstance(data, dict) else data
    if not children:
        return f"_No LVM volume groups on `{params.node}`._"

    lines = [f"## LVM on `{params.node}`", ""]
    for vg in children:
        name = vg.get("name", "?")
        size = vg.get("size", 0)
        free = vg.get("free", 0)
        used = (size or 0) - (free or 0)
        lines.append(
            f"### VG `{name}` — {fmt_bytes(size)} total, "
            f"{fmt_bytes(used)} used ({_pct(used, size)}), "
            f"{fmt_bytes(free)} free"
        )
        members = vg.get("children", []) or []
        pvs = [m for m in members if m.get("leaf") in (0, 1) and m.get("name", "").startswith("/dev/")]
        lvs = [m for m in members if m not in pvs]

        if pvs:
            lines.append("- **PVs**:")
            for pv in pvs:
                lines.append(
                    f"  - `{pv.get('name', '?')}` — "
                    f"{fmt_bytes(pv.get('size', 0))}"
                )
        if lvs:
            lines.append("- **LVs**:")
            for lv in lvs:
                lv_name = lv.get("name", "?")
                lv_size = fmt_bytes(lv.get("size", 0))
                lv_type = lv.get("lv_type") or lv.get("type") or ""
                suffix = f" ({lv_type})" if lv_type else ""
                lines.append(f"  - `{lv_name}` — {lv_size}{suffix}")
        lines.append("")

    return "\n".join(lines).rstrip()


@mcp.tool(
    name="proxmox_list_lvm_thin",
    annotations={
        "title": "List LVM-thin Pools",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_list_lvm_thin(params: NodeInput) -> str:
    """List LVM-thin pools on a node.

    A thin pool is the storage type used by the default `local-lvm` in
    most Proxmox installations.

    Returns:
        str: For each thin pool: VG, name, size, used %.
    """
    cfg = require_config()
    if cfg:
        return cfg

    try:
        pools = await http_client.get(f"/nodes/{params.node}/disks/lvmthin")
    except Exception as exc:
        return http_client.format_http_error(exc)

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(pools, indent=2, default=str)

    if not pools:
        return f"_No LVM-thin pools on `{params.node}`._"

    lines = [
        f"## LVM-thin pools on `{params.node}`",
        "",
        "| VG | Pool | Size | Used | Metadata used |",
        "| --- | --- | --- | --- | --- |",
    ]
    for p in pools:
        vg = p.get("vg", "?")
        name = p.get("lv", p.get("name", "?"))
        size = fmt_bytes(p.get("lv_size", p.get("size", 0)))
        used_pct = p.get("used_percent")
        meta_pct = p.get("metadata_percent")
        used_str = f"{used_pct:.1f}%" if isinstance(used_pct, (int, float)) else "?"
        meta_str = f"{meta_pct:.1f}%" if isinstance(meta_pct, (int, float)) else "?"
        lines.append(f"| {vg} | `{name}` | {size} | {used_str} | {meta_str} |")

    return "\n".join(lines)
