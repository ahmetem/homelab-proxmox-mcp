"""Phase 2: LVM and LVM-thin pool creation/removal.

Backed by Proxmox REST endpoints:
  - POST   /nodes/{node}/disks/lvm        body: name, device, add_storage
  - DELETE /nodes/{node}/disks/lvm/{name} query: cleanup-config, cleanup-disks
  - POST   /nodes/{node}/disks/lvmthin    body: name, device, add_storage
  - DELETE /nodes/{node}/disks/lvmthin/{name}?volume-group=...

Both creation endpoints have an ``add_storage`` flag that registers the new
VG/pool as a cluster storage entry in one shot.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import missing_confirm, missing_data_loss_ack
from proxmox_mcp.mcp_instance import mcp


_VG_NAME = r"^[A-Za-z0-9_+.][A-Za-z0-9_+.-]*$"


class LvmCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., min_length=1)
    name: str = Field(
        ...,
        description="Volume group name (alphanumeric, underscore, dash).",
        min_length=1, max_length=64, pattern=_VG_NAME,
    )
    device: str = Field(
        ...,
        description=(
            "Block device to use as the PV. Whole disk (e.g. '/dev/nvme0n1') "
            "or partition. Disk must be empty — wipe first if needed."
        ),
        min_length=1, max_length=64,
        pattern=r"^/dev/[A-Za-z0-9/_-]+$",
    )
    add_storage: bool = Field(default=True, description="If true (default), also register the VG as a Proxmox storage.")
    confirm: bool = Field(default=False)
    reason: Optional[str] = Field(default=None, max_length=200)


class LvmThinCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., min_length=1)
    name: str = Field(
        ...,
        description="Thin pool name (also used as PVE storage ID by default).",
        min_length=1, max_length=64, pattern=_VG_NAME,
    )
    device: str = Field(
        ...,
        description="Block device to host the thin pool. Disk must be empty.",
        min_length=1, max_length=64,
        pattern=r"^/dev/[A-Za-z0-9/_-]+$",
    )
    add_storage: bool = Field(default=True)
    confirm: bool = Field(default=False)
    reason: Optional[str] = Field(default=None, max_length=200)


class LvmDestroyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., min_length=1)
    name: str = Field(..., description="Volume group name to destroy.", min_length=1, max_length=64, pattern=_VG_NAME)
    cleanup_config: bool = Field(default=True, description="If true, also remove matching PVE storage configuration entries.")
    cleanup_disks: bool = Field(
        default=False,
        description=(
            "If true, also wipe the underlying disks after removing the VG. "
            "Extra dangerous — set only when you actually want to free the disks."
        ),
    )
    confirm: bool = Field(default=False)
    i_understand_data_loss: bool = Field(default=False)
    reason: Optional[str] = Field(default=None, max_length=200)


class LvmThinDestroyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1, max_length=64, pattern=_VG_NAME)
    volume_group: str = Field(
        ...,
        description="VG that contains the thin pool.",
        min_length=1, max_length=64, pattern=_VG_NAME,
    )
    cleanup_config: bool = Field(default=True)
    cleanup_disks: bool = Field(default=False)
    confirm: bool = Field(default=False)
    i_understand_data_loss: bool = Field(default=False)
    reason: Optional[str] = Field(default=None, max_length=200)


@mcp.tool(
    name="proxmox_create_lvm_vg",
    annotations={
        "title": "Create LVM Volume Group",
        "readOnlyHint": False, "destructiveHint": True,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_create_lvm_vg(params: LvmCreateInput) -> str:
    """Create an LVM volume group on a device, optionally registering it as PVE storage.

    The device must be empty (no partition table, no FS signatures). Use
    proxmox_wipe_disk first if needed. Requires confirm=true.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_create_lvm_vg")

    payload = {"name": params.name, "device": params.device, "add_storage": 1 if params.add_storage else 0}
    try:
        task_id = await http_client.post(f"/nodes/{params.node}/disks/lvm", data=payload)
    except Exception as exc:
        return http_client.format_http_error(exc)

    storage_msg = " Also registered as PVE storage." if params.add_storage else ""
    return (
        f"OK: VG '{params.name}' creation started on `{params.device}` "
        f"({params.node}).{storage_msg} Task: {task_id}"
    )


@mcp.tool(
    name="proxmox_create_lvm_thin",
    annotations={
        "title": "Create LVM-thin Pool",
        "readOnlyHint": False, "destructiveHint": True,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_create_lvm_thin(params: LvmThinCreateInput) -> str:
    """Create an LVM-thin pool on a device, optionally registering it as PVE storage.

    Requires confirm=true. Device must be empty.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_create_lvm_thin")

    payload = {"name": params.name, "device": params.device, "add_storage": 1 if params.add_storage else 0}
    try:
        task_id = await http_client.post(f"/nodes/{params.node}/disks/lvmthin", data=payload)
    except Exception as exc:
        return http_client.format_http_error(exc)

    storage_msg = " Also registered as PVE storage." if params.add_storage else ""
    return (
        f"OK: Thin pool '{params.name}' creation started on `{params.device}` "
        f"({params.node}).{storage_msg} Task: {task_id}"
    )


@mcp.tool(
    name="proxmox_destroy_lvm_vg",
    annotations={
        "title": "Destroy LVM Volume Group (DESTROY ALL DATA)",
        "readOnlyHint": False, "destructiveHint": True,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_destroy_lvm_vg(params: LvmDestroyInput) -> str:
    """Destroy an LVM volume group and all logical volumes inside it.

    Requires BOTH confirm=true AND i_understand_data_loss=true.

    All LVs and their data are irretrievable. If cleanup_disks=true, the
    underlying disk is also wiped (use only when you want to repurpose it).
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_destroy_lvm_vg")
    if not params.i_understand_data_loss:
        return missing_data_loss_ack("proxmox_destroy_lvm_vg")

    query = {"cleanup-config": 1 if params.cleanup_config else 0, "cleanup-disks": 1 if params.cleanup_disks else 0}
    try:
        task_id = await http_client.delete(f"/nodes/{params.node}/disks/lvm/{params.name}", params=query)
    except Exception as exc:
        return http_client.format_http_error(exc)

    return f"OK: Destroy of VG '{params.name}' started ({params.node}). Task: {task_id}"


@mcp.tool(
    name="proxmox_destroy_lvm_thin",
    annotations={
        "title": "Destroy LVM-thin Pool (DESTROY ALL DATA)",
        "readOnlyHint": False, "destructiveHint": True,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_destroy_lvm_thin(params: LvmThinDestroyInput) -> str:
    """Destroy an LVM-thin pool.

    Requires BOTH confirm=true AND i_understand_data_loss=true.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_destroy_lvm_thin")
    if not params.i_understand_data_loss:
        return missing_data_loss_ack("proxmox_destroy_lvm_thin")

    query = {
        "volume-group": params.volume_group,
        "cleanup-config": 1 if params.cleanup_config else 0,
        "cleanup-disks": 1 if params.cleanup_disks else 0,
    }
    try:
        task_id = await http_client.delete(f"/nodes/{params.node}/disks/lvmthin/{params.name}", params=query)
    except Exception as exc:
        return http_client.format_http_error(exc)

    return f"OK: Destroy of thin pool '{params.name}' started ({params.node}). Task: {task_id}"
