"""VM and container lifecycle / status / resize tools."""
from __future__ import annotations

import time
from typing import Any, Optional

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import (
    compact_json,
    fmt_bytes,
    fmt_uptime,
    missing_confirm,
    status_icon,
)
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import (
    ResponseFormat,
    VMInput,
    VMListInput,
    VMPowerInput,
    VMResizeInput,
)


@mcp.tool(
    name="proxmox_list_vms",
    annotations={
        "title": "List VMs and Containers",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_list_vms(params: VMListInput = VMListInput()) -> str:
    """List virtual machines and LXC containers across the cluster.

    Optional filters: node, vmid, status ('running'/'stopped'),
    guest_type ('qemu'/'lxc'). Use them to avoid dumping the full guest list.
    """
    cfg = require_config()
    if cfg:
        return cfg
    try:
        vms = await http_client.get("/cluster/resources", params={"type": "vm"})
    except Exception as exc:
        return http_client.format_http_error(exc)

    vms = [
        v for v in (vms or [])
        if (params.node is None or v.get("node") == params.node)
        and (params.vmid is None or v.get("vmid") == params.vmid)
        and (params.status is None or v.get("status") == params.status)
        and (params.guest_type is None or v.get("type") == params.guest_type)
    ]

    if params.response_format == ResponseFormat.JSON:
        return compact_json(vms, fields=params.fields)

    if not vms:
        return "_No matching VMs or containers._"

    lines = ["## Virtual Machines and Containers", ""]
    for v in sorted(vms, key=lambda x: x.get("vmid", 0)):
        icon = status_icon(v.get("status", "?"))
        vmtype = "\U0001F4E6 LXC" if v.get("type") == "lxc" else "\U0001F4BB VM"
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
    """Get detailed runtime status of a specific VM or LXC container."""
    cfg = require_config()
    if cfg:
        return cfg
    actual_type = await _resolve_vm_type(params.node, params.vmid, hint=params.vm_type)
    try:
        status = await http_client.get(
            f"/nodes/{params.node}/{actual_type}/{params.vmid}/status/current"
        )
    except Exception as exc:
        return http_client.format_http_error(exc)

    if params.response_format == ResponseFormat.JSON:
        return compact_json(status, fields=params.fields)

    icon = status_icon(status.get("status", "?"))
    vmtype = "LXC Container" if actual_type == "lxc" else "QEMU VM"
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


# vmid -> (guest type, monotonic timestamp). Power actions and status reads
# both need the qemu/lxc distinction; a short TTL avoids re-fetching the whole
# cluster resource list on every call in a burst of related operations.
_VM_TYPE_CACHE: dict[int, tuple[str, float]] = {}
_VM_TYPE_TTL = 30.0


async def _resolve_vm_type(node: str, vmid: int, hint: str = "qemu") -> str:
    """Detect whether vmid is a 'qemu' VM or an 'lxc' container by querying the
    cluster resource list (cached for a short TTL). Falls back to `hint` if
    detection fails, so the result is never worse than the caller-supplied
    type. This prevents a wrong or defaulted vm_type from misrouting an action
    (e.g. starting an LXC via the qemu endpoint, which silently no-ops)."""
    cached = _VM_TYPE_CACHE.get(vmid)
    if cached and time.monotonic() - cached[1] < _VM_TYPE_TTL:
        return cached[0]
    try:
        resources = await http_client.get("/cluster/resources", params={"type": "vm"})
        now = time.monotonic()
        found: Optional[str] = None
        for r in resources or []:
            t = r.get("type")
            r_vmid = r.get("vmid")
            if t in ("qemu", "lxc") and isinstance(r_vmid, int):
                _VM_TYPE_CACHE[r_vmid] = (t, now)
                if r_vmid == vmid and (not node or r.get("node") == node):
                    found = t
        if found:
            return found
    except Exception:
        pass
    return hint if hint in ("qemu", "lxc") else "qemu"


@mcp.tool(
    name="proxmox_vm_power",
    annotations={
        "title": "VM/Container Power Action (start/shutdown/stop/reboot)",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def proxmox_vm_power(params: VMPowerInput) -> str:
    """Power action on a VM or LXC container. Requires confirm=true.

    Actions: 'start', 'shutdown' (graceful ACPI), 'stop' (pull-the-plug,
    may cause data loss), 'reboot'. The guest type (qemu/lxc) is
    auto-detected from vmid. Set wait_seconds>0 to poll the task and get the
    final result inline instead of just a task ID.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_vm_power")

    actual_type = await _resolve_vm_type(params.node, params.vmid, hint=params.vm_type)
    try:
        task_id = await http_client.post(
            f"/nodes/{params.node}/{actual_type}/{params.vmid}/status/{params.action}"
        )
    except Exception as exc:
        return http_client.format_http_error(exc)

    note = ""
    if actual_type != params.vm_type:
        note = f" (auto-detected '{actual_type}', given '{params.vm_type}')"
    suffix = await http_client.wait_for_task(params.node, task_id, params.wait_seconds)
    if not suffix:
        suffix = " Use proxmox_get_vm_status to confirm new state."
    return (
        f"OK: Action '{params.action}' on {actual_type} {params.vmid} "
        f"accepted{note}. Task: {task_id}.{suffix}"
    )


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
    Stopped guests change immediately; running guests hot-resize when possible,
    otherwise the value applies on next reboot (QEMU RAM hotplug often needs
    to be enabled in the VM config).
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
        "a reboot may be required (use proxmox_vm_power action='reboot')."
    )
    return msg
