"""Phase 3: VM disk movement / cloning / ISO listing.

Backed by Proxmox REST endpoints:
  - POST /nodes/{node}/qemu/{vmid}/move_disk   (QEMU)
  - POST /nodes/{node}/lxc/{vmid}/move_volume  (LXC)
  - POST /nodes/{node}/qemu/{vmid}/clone       (QEMU)
  - POST /nodes/{node}/lxc/{vmid}/clone        (LXC)
  - GET  /nodes/{node}/storage/{storage}/content?content=iso

move_disk runs as a background task — VM can stay running for most storages
(live migration of disk). For LXC, the container is briefly paused.
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import fmt_bytes, missing_confirm
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import ResponseFormat


_STORAGE_ID = r"^[A-Za-z][A-Za-z0-9_.-]*$"


class MoveDiskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., min_length=1)
    vmid: int = Field(..., ge=100, le=999999999)
    vm_type: str = Field(
        default="qemu",
        pattern="^(qemu|lxc)$",
        description="VM type: 'qemu' for VMs, 'lxc' for containers.",
    )
    disk: str = Field(
        ...,
        description=(
            "Disk identifier as it appears in the VM config "
            "(e.g. 'scsi0', 'virtio0', 'ide0' for QEMU; "
            "'rootfs', 'mp0' for LXC)."
        ),
        min_length=1, max_length=32,
        pattern=r"^[a-z]+[0-9]*$",
    )
    target_storage: str = Field(
        ...,
        description="Destination storage ID (e.g. 'nvmepool', 'local-lvm').",
        min_length=1, max_length=64, pattern=_STORAGE_ID,
    )
    format: Optional[str] = Field(
        default=None,
        description=(
            "Optional target format: raw, qcow2, vmdk. Most storages "
            "infer this from their backend; leave empty unless you "
            "need to force it."
        ),
        pattern=r"^(raw|qcow2|vmdk)$",
    )
    delete_source: bool = Field(
        default=True,
        description=(
            "If true (default), the source disk is removed after the copy "
            "completes. Set false to keep the original as an unused disk."
        ),
    )
    confirm: bool = Field(default=False)
    reason: Optional[str] = Field(default=None, max_length=200)


class CloneVmInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., min_length=1)
    vmid: int = Field(..., ge=100, le=999999999, description="Source VM/CT ID to clone from.")
    newid: int = Field(..., ge=100, le=999999999, description="New VM/CT ID for the clone.")
    vm_type: str = Field(default="qemu", pattern="^(qemu|lxc)$")
    name: Optional[str] = Field(
        default=None,
        description="Hostname/name of the new VM/CT.",
        min_length=1, max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    target_storage: Optional[str] = Field(
        default=None,
        description=(
            "Place cloned disks on this storage. Omit to keep them on "
            "the source storage."
        ),
        min_length=1, max_length=64, pattern=_STORAGE_ID,
    )
    full: bool = Field(
        default=False,
        description=(
            "Full clone (independent copy) vs linked clone (shares base "
            "with source, faster + less space, source must stay alive). "
            "Linked clone requires the source to have a snapshot on "
            "snapshot-capable storage."
        ),
    )
    snapname: Optional[str] = Field(
        default=None,
        description="Clone from this snapshot instead of the current state.",
        min_length=1, max_length=40,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$",
    )
    description: Optional[str] = Field(default=None, max_length=200)
    confirm: bool = Field(default=False)
    reason: Optional[str] = Field(default=None, max_length=200)


class IsoListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., min_length=1)
    storage: str = Field(
        default="local",
        description="Storage holding ISOs (default 'local').",
        min_length=1, max_length=64, pattern=_STORAGE_ID,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


@mcp.tool(
    name="proxmox_move_disk",
    annotations={
        "title": "Move VM/CT Disk to Another Storage",
        "readOnlyHint": False, "destructiveHint": False,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_move_disk(params: MoveDiskInput) -> str:
    """Move a VM/CT disk from its current storage to a different storage.

    For QEMU VMs this is a live operation — the VM keeps running while the
    disk is copied; Proxmox uses block-level mirroring then atomically
    switches over. For LXC containers the container is briefly paused.

    Examples:
      - Move scsi0 of VM 102 from vmdata to nvmepool:
        node=pve, vmid=102, vm_type=qemu, disk=scsi0, target_storage=nvmepool
      - Keep the original copy as an unused disk: delete_source=false

    Requires confirm=true.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_move_disk")

    if params.vm_type == "qemu":
        path = f"/nodes/{params.node}/qemu/{params.vmid}/move_disk"
        payload = {
            "disk": params.disk,
            "storage": params.target_storage,
            "delete": 1 if params.delete_source else 0,
        }
        if params.format:
            payload["format"] = params.format
    else:
        path = f"/nodes/{params.node}/lxc/{params.vmid}/move_volume"
        payload = {
            "volume": params.disk,
            "storage": params.target_storage,
            "delete": 1 if params.delete_source else 0,
        }

    try:
        task_id = await http_client.post(path, data=payload)
    except Exception as exc:
        return http_client.format_http_error(exc)

    return (
        f"OK: Disk move started — {params.vm_type} {params.vmid} "
        f"`{params.disk}` → storage `{params.target_storage}`. "
        f"Task: {task_id}. The operation runs in background; "
        "large disks can take many minutes. VM stays online for QEMU."
    )


@mcp.tool(
    name="proxmox_clone_vm",
    annotations={
        "title": "Clone VM/Container",
        "readOnlyHint": False, "destructiveHint": False,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_clone_vm(params: CloneVmInput) -> str:
    """Clone a VM or LXC container to a new ID.

    Linked clones (full=false) share storage with the source via snapshots —
    fast to create, save space, but require the source to stay around and
    require snapshot-capable storage (ZFS, qcow2, btrfs).

    Full clones (full=true) are independent copies — slower but standalone.

    Use snapname to clone from a specific snapshot rather than the live state.

    Requires confirm=true.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_clone_vm")
    if params.newid == params.vmid:
        return "Error: newid must differ from source vmid."

    path = f"/nodes/{params.node}/{params.vm_type}/{params.vmid}/clone"
    payload: dict = {"newid": params.newid}
    if params.name:
        if params.vm_type == "qemu":
            payload["name"] = params.name
        else:
            payload["hostname"] = params.name
    if params.target_storage:
        payload["storage"] = params.target_storage
    if params.full:
        payload["full"] = 1
    if params.snapname:
        payload["snapname"] = params.snapname
    if params.description:
        payload["description"] = params.description

    try:
        task_id = await http_client.post(path, data=payload)
    except Exception as exc:
        return http_client.format_http_error(exc)

    mode = "full clone" if params.full else "linked clone"
    return (
        f"OK: {mode.capitalize()} started — {params.vm_type} {params.vmid} "
        f"→ {params.newid}. Task: {task_id}. "
        "Use proxmox_get_vm_status on the new ID once the task completes."
    )


@mcp.tool(
    name="proxmox_list_isos",
    annotations={
        "title": "List ISO Images",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def proxmox_list_isos(params: IsoListInput) -> str:
    """List ISO images stored on a content-storage.

    Returns:
        str: For each ISO: filename, size, creation time, volid.
    """
    cfg = require_config()
    if cfg:
        return cfg
    try:
        items = await http_client.get(
            f"/nodes/{params.node}/storage/{params.storage}/content",
            params={"content": "iso"},
        )
    except Exception as exc:
        return http_client.format_http_error(exc)

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(items, indent=2, default=str)

    if not items:
        return f"_No ISO images on `{params.storage}`._"

    lines = [
        f"## ISOs on `{params.storage}` (node `{params.node}`)",
        "",
        "| Filename | Size | Created | volid |",
        "| --- | --- | --- | --- |",
    ]
    for it in sorted(items, key=lambda x: x.get("volid", "")):
        volid = it.get("volid", "?")
        fname = volid.split("/", 1)[-1] if "/" in volid else volid
        size = fmt_bytes(it.get("size", 0))
        ctime = it.get("ctime", 0)
        try:
            ts = _dt.datetime.fromtimestamp(int(ctime)).strftime("%Y-%m-%d")
        except Exception:
            ts = "?"
        lines.append(f"| `{fname}` | {size} | {ts} | `{volid}` |")
    return "\n".join(lines)
