"""VM and container lifecycle / status / resize tools."""
from __future__ import annotations

import json
from typing import Any

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import fmt_bytes, fmt_uptime, missing_confirm, status_icon
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import (
    FormatInput,
    ResponseFormat,
    VMActionInput,
    VMInput,
    VMResizeInput,
)


@mcp.tool(
    name="proxmox_list_vms",
    annotations={
        "title": "List All VMs and Containers",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_list_vms(params: FormatInput) -> str:
    """List all virtual machines and LXC containers across the cluster.

    Returns:
        str: For each VM/CT: ID, name, type, node, status, CPU, memory.
    """
    cfg = require_config()
    if cfg:
        return cfg
    try:
        vms = await http_client.get("/cluster/resources", params={"type": "vm"})
    except Exception as exc:
        return http_client.format_http_error(exc)

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(vms, indent=2, default=str)

    if not vms:
        return "_No VMs or containers found._"

    lines = ["## Virtual Machines and Containers", ""]
    for v in sorted(vms, key=lambda x: x.get("vmid", 0)):
        icon = status_icon(v.get("status", "?"))
        vmtype = "\ud83d\udce6 LXC" if v.get("type") == "lxc" else "\ud83d\udcbb VM"
        name = v.get("name", "?")
        vmid = v.get("vmid", "?")
        node = v.get("node", "?")
        cpu = (v.get("cpu") or 0) * 100
        mem_used = fmt_bytes(v.get("mem", 0))
        mem_total = fmt_bytes(v.get("maxmem", 0))
        uptime = fmt_uptime(v.get("uptime", 0))
        lines.append(
            f"- {icon} {vmtype} **{vmid}** `{name}` on `{node}` — "
            f"{v.get('status')} — uptime: {uptime}, "
            f"CPU: {cpu:.1f}%, Mem: {mem_used}/{mem_total}"
        )
    return "\n".join(lines)


@mcp.tool(
    name="proxmox_get_vm_status",
    annotations={
        "title": "Get VM/Container Detailed Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_get_vm_status(params: VMInput) -> str:
    """Get detailed status of a specific VM or LXC container.

    Returns:
        str: Detailed runtime metrics for the VM/container.
    """
    cfg = require_config()
    if cfg:
        return cfg
    try:
        status = await http_client.get(
            f"/nodes/{params.node}/{params.vm_type}/{params.vmid}/status/current"
        )
    except Exception as exc:
        return http_client.format_http_error(exc)

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(status, indent=2, default=str)

    icon = status_icon(status.get("status", "?"))
    vmtype = "LXC Container" if params.vm_type == "lxc" else "QEMU VM"
    lines = [
        f"## {icon} {vmtype} {params.vmid} `{status.get('name', '?')}`",
        "",
        f"- **Node**: `{params.node}`",
        f"- **Status**: {status.get('status', '?')}",
        f"- **Uptime**: {fmt_uptime(status.get('uptime', 0))}",
        f"- **CPU**: {(status.get('cpu') or 0) * 100:.1f}% of "
        f"{status.get('cpus', '?')} cores",
        f"- **Memory**: {fmt_bytes(status.get('mem'))} / "
        f"{fmt_bytes(status.get('maxmem'))}",
        f"- **Disk read**: {fmt_bytes(status.get('diskread'))}",
        f"- **Disk write**: {fmt_bytes(status.get('diskwrite'))}",
        f"- **Network in**: {fmt_bytes(status.get('netin'))}",
        f"- **Network out**: {fmt_bytes(status.get('netout'))}",
    ]
    if status.get("agent"):
        lines.append(f"- **Guest agent**: enabled")
    if status.get("ha", {}).get("managed"):
        lines.append(f"- **HA managed**: yes")
    return "\n".join(lines)


async def _vm_action(node: str, vmid: int, vm_type: str, action: str) -> str:
    """Send a power action to a VM/CT. Returns result message."""
    try:
        task_id = await http_client.post(
            f"/nodes/{node}/{vm_type}/{vmid}/status/{action}"
        )
    except Exception as exc:
        return http_client.format_http_error(exc)
    return (
        f"OK: Action '{action}' on {vm_type} {vmid} accepted. "
        f"Task ID: {task_id}. "
        "Use proxmox_get_vm_status to confirm new state."
    )


@mcp.tool(
    name="proxmox_vm_start",
    annotations={
        "title": "Start VM/Container",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_vm_start(params: VMActionInput) -> str:
    """Start a VM or LXC container. Requires confirm=true."""
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_vm_start")
    return await _vm_action(params.node, params.vmid, params.vm_type, "start")


@mcp.tool(
    name="proxmox_vm_shutdown",
    annotations={
        "title": "Graceful Shutdown",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def proxmox_vm_shutdown(params: VMActionInput) -> str:
    """Gracefully shutdown a VM or LXC container via ACPI. Requires confirm=true."""
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_vm_shutdown")
    return await _vm_action(params.node, params.vmid, params.vm_type, "shutdown")


@mcp.tool(
    name="proxmox_vm_stop",
    annotations={
        "title": "Force Stop VM/Container",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_vm_stop(params: VMActionInput) -> str:
    """Force stop (pull-the-plug) a VM or container. May cause data loss. Requires confirm=true."""
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_vm_stop")
    return await _vm_action(params.node, params.vmid, params.vm_type, "stop")


@mcp.tool(
    name="proxmox_vm_reboot",
    annotations={
        "title": "Reboot VM/Container",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def proxmox_vm_reboot(params: VMActionInput) -> str:
    """Reboot a VM or container (graceful then power-cycle). Requires confirm=true."""
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_vm_reboot")
    return await _vm_action(params.node, params.vmid, params.vm_type, "reboot")


@mcp.tool(
    name="proxmox_resize_vm",
    annotations={
        "title": "Resize VM/Container RAM and/or CPU",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_resize_vm(params: VMResizeInput) -> str:
    """Change RAM and/or CPU core count of a VM or LXC container.

    Requires confirm=true. At least one of memory_mb or cores must be provided.

    Behavior:
      - If the VM is stopped, change is immediate.
      - If the VM is running, Proxmox attempts hot-resize. This usually works
        for adding resources; reducing may need a reboot.
      - If hot-resize cannot apply, the new value is saved and takes effect
        on the next reboot. The task result indicates the situation.

    For QEMU VMs, RAM hotplug may require the VM to have memory hotplug
    enabled in its config (often it isn't by default); if so, a reboot
    is needed for the new RAM to be visible to the guest OS.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_resize_vm")
    if params.memory_mb is None and params.cores is None:
        return "Error: Provide at least one of memory_mb or cores."

    payload: dict[str, Any] = {}
    if params.memory_mb is not None:
        payload["memory"] = params.memory_mb
    if params.cores is not None:
        payload["cores"] = params.cores

    path = f"/nodes/{params.node}/{params.vm_type}/{params.vmid}/config"
    try:
        result = await http_client.put(path, data=payload)
    except Exception as exc:
        return http_client.format_http_error(exc)

    changes = []
    if params.memory_mb is not None:
        changes.append(f"memory={params.memory_mb} MB")
    if params.cores is not None:
        changes.append(f"cores={params.cores}")

    msg = (
        f"OK: Config update applied to {params.vm_type} {params.vmid}: "
        f"{', '.join(changes)}."
    )
    if result:
        msg += f" Task: {result}."
    msg += (
        " If the VM was running and the guest OS does not reflect the change, "
        "a reboot may be required (use proxmox_vm_reboot)."
    )
    return msg
