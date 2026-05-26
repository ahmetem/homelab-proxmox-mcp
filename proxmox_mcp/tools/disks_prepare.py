"""Phase 2: disk preparation (wipe + GPT init).

All operations here are destructive: they erase metadata or partition tables
and require both ``confirm=true`` and ``i_understand_data_loss=true`` for wipe.
Init-GPT only requires confirm because Proxmox refuses to overwrite a disk
that already carries usable data.

Backed by Proxmox REST endpoints:
  - PUT /nodes/{node}/disks/initgpt
  - PUT /nodes/{node}/disks/wipedisk        (Proxmox 8.0+)
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import missing_confirm, missing_data_loss_ack
from proxmox_mcp.mcp_instance import mcp


class DiskInitGptInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., description="Node name", min_length=1)
    disk: str = Field(
        ...,
        description="Block device path (e.g. '/dev/sdX', '/dev/nvme0n1').",
        min_length=1,
        max_length=64,
        pattern=r"^/dev/[A-Za-z0-9/_-]+$",
    )
    uuid: Optional[str] = Field(
        default=None,
        description="Optional disk UUID for safety. If set, must match the actual disk.",
        max_length=64,
    )
    confirm: bool = Field(
        default=False,
        description="Must be true to execute. Only set after explicit user confirmation.",
    )
    reason: Optional[str] = Field(
        default=None, description="Optional note about why", max_length=200
    )


class DiskWipeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., description="Node name", min_length=1)
    disk: str = Field(
        ...,
        description="Block device path (e.g. '/dev/sdX', '/dev/nvme0n1').",
        min_length=1,
        max_length=64,
        pattern=r"^/dev/[A-Za-z0-9/_-]+$",
    )
    confirm: bool = Field(
        default=False,
        description="Must be true to execute. Only set after explicit user confirmation.",
    )
    i_understand_data_loss: bool = Field(
        default=False,
        description=(
            "Must be true. Wiping a disk erases its partition table and "
            "filesystem signatures — all data on the disk is irretrievable."
        ),
    )
    reason: Optional[str] = Field(
        default=None, description="Optional note about why", max_length=200
    )


@mcp.tool(
    name="proxmox_disk_init_gpt",
    annotations={
        "title": "Initialize Disk with GPT Partition Table",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_disk_init_gpt(params: DiskInitGptInput) -> str:
    """Write a fresh GPT partition table to a disk.

    Proxmox refuses this if the disk is already in use (mounted, in LVM/ZFS,
    has a recognizable filesystem). Wipe the disk first via proxmox_wipe_disk
    if you need to clobber existing data.

    Requires confirm=true.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_disk_init_gpt")

    payload: dict = {"disk": params.disk}
    if params.uuid:
        payload["uuid"] = params.uuid

    try:
        task_id = await http_client.put(
            f"/nodes/{params.node}/disks/initgpt", data=payload
        )
    except Exception as exc:
        return http_client.format_http_error(exc)

    return (
        f"OK: GPT init started on `{params.disk}` ({params.node}). "
        f"Task: {task_id}"
    )


@mcp.tool(
    name="proxmox_wipe_disk",
    annotations={
        "title": "Wipe Disk (DESTROY ALL DATA)",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_wipe_disk(params: DiskWipeInput) -> str:
    """Erase partition table and filesystem signatures on a disk.

    Requires BOTH confirm=true AND i_understand_data_loss=true.

    All data on the disk is irretrievable after this completes. Proxmox runs
    `wipefs -a` plus zeroes the first/last few sectors. Disks currently in
    use by mounted filesystems, LVM, or imported ZFS pools cannot be wiped
    until they are taken out of service first.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_wipe_disk")
    if not params.i_understand_data_loss:
        return missing_data_loss_ack("proxmox_wipe_disk")

    payload = {"disk": params.disk}
    try:
        task_id = await http_client.put(
            f"/nodes/{params.node}/disks/wipedisk", data=payload
        )
    except Exception as exc:
        return http_client.format_http_error(exc)

    return (
        f"OK: Wipe started on `{params.disk}` ({params.node}). "
        f"Task: {task_id}. Verify with proxmox_list_disks once the task completes."
    )
