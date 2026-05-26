"""Node-level read-only tools."""
from __future__ import annotations

import json

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import fmt_bytes, fmt_uptime, status_icon
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import FormatInput, NodeInput, ResponseFormat


@mcp.tool(
    name="proxmox_list_nodes",
    annotations={
        "title": "List Proxmox Nodes",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_list_nodes(params: FormatInput) -> str:
    """List all nodes in the Proxmox cluster with status and resource usage.

    Returns:
        str: For each node: name, status, uptime, CPU, memory.
    """
    cfg = require_config()
    if cfg:
        return cfg
    try:
        nodes = await http_client.get("/nodes")
    except Exception as exc:
        return http_client.format_http_error(exc)

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(nodes, indent=2, default=str)

    if not nodes:
        return "_No nodes found._"

    lines = ["## Proxmox Cluster Nodes", ""]
    for n in nodes:
        icon = status_icon(n.get("status", "?"))
        cpu_pct = (n.get("cpu") or 0) * 100
        mem_used = fmt_bytes(n.get("mem", 0))
        mem_total = fmt_bytes(n.get("maxmem", 0))
        uptime = fmt_uptime(n.get("uptime", 0))
        lines.append(
            f"- {icon} **{n.get('node')}** — {n.get('status')} — "
            f"uptime: {uptime}, CPU: {cpu_pct:.1f}%, "
            f"Mem: {mem_used}/{mem_total}"
        )
    return "\n".join(lines)


@mcp.tool(
    name="proxmox_get_node_status",
    annotations={
        "title": "Get Node Status (Detailed)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_get_node_status(params: NodeInput) -> str:
    """Get detailed status for a node: CPU, memory, disk, load average, kernel.

    Returns:
        str: Detailed node metrics in markdown or JSON.
    """
    cfg = require_config()
    if cfg:
        return cfg
    try:
        status = await http_client.get(f"/nodes/{params.node}/status")
    except Exception as exc:
        return http_client.format_http_error(exc)

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(status, indent=2, default=str)

    cpu = status.get("cpuinfo", {}) or {}
    mem = status.get("memory", {}) or {}
    swap = status.get("swap", {}) or {}
    rootfs = status.get("rootfs", {}) or {}
    load = status.get("loadavg", []) or []

    lines = [f"## Node `{params.node}` Status", ""]
    lines.append(f"- **Uptime**: {fmt_uptime(status.get('uptime', 0))}")
    lines.append(f"- **Kernel**: {status.get('kversion', '?')}")
    lines.append(f"- **PVE version**: {status.get('pveversion', '?')}")
    lines.append(
        f"- **CPU**: {cpu.get('model', '?')} — "
        f"{cpu.get('cpus', '?')} cores @ {cpu.get('mhz', '?')} MHz"
    )
    lines.append(f"- **CPU usage**: {(status.get('cpu') or 0) * 100:.1f}%")
    lines.append(
        f"- **Memory**: {fmt_bytes(mem.get('used'))} / "
        f"{fmt_bytes(mem.get('total'))}"
    )
    if swap.get("total"):
        lines.append(
            f"- **Swap**: {fmt_bytes(swap.get('used'))} / "
            f"{fmt_bytes(swap.get('total'))}"
        )
    if rootfs.get("total"):
        lines.append(
            f"- **Root FS**: {fmt_bytes(rootfs.get('used'))} / "
            f"{fmt_bytes(rootfs.get('total'))}"
        )
    if load:
        lines.append(f"- **Load avg**: {', '.join(str(x) for x in load)}")
    return "\n".join(lines)
