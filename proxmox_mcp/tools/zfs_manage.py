"""Phase 2: ZFS pool create / destroy (single manage tool).

Backed by Proxmox REST endpoints:
  - POST   /nodes/{node}/disks/zfs        body: name, devices, raidlevel,
                                                ashift, compression, add_storage
  - DELETE /nodes/{node}/disks/zfs/{name} query: cleanup-config, cleanup-disks

raidlevel values accepted by Proxmox 8.x / 9.x:
  single, mirror, raid10, raidz, raidz2, raidz3, draid, draid2, draid3
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import missing_confirm, missing_data_loss_ack
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import WAIT_DESC


_POOL_NAME = r"^[A-Za-z][A-Za-z0-9_.-]*$"

_RAID_MIN_DEVICES = {
    "single": 1,
    "mirror": 2,
    "raid10": 4,
    "raidz": 3,
    "raidz2": 4,
    "raidz3": 5,
    "draid": 3,
    "draid2": 4,
    "draid3": 5,
}


class ZfsPoolManageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(
        ...,
        description=(
            "'create' (needs devices; confirm) or 'destroy' "
            "(confirm + i_understand_data_loss — all data lost)."
        ),
        pattern="^(create|destroy)$",
    )
    node: str = Field(..., min_length=1)
    name: str = Field(
        ..., description="ZFS pool name (must start with letter).",
        min_length=1, max_length=64, pattern=_POOL_NAME,
    )
    devices: Optional[list[str]] = Field(
        default=None,
        description=(
            "create only: block devices for the pool. All must be empty — "
            "wipe first if needed."
        ),
        max_length=64,
    )
    raidlevel: str = Field(
        default="single",
        description=(
            "create only: single, mirror, raid10, raidz, raidz2, raidz3, "
            "draid, draid2, draid3."
        ),
        pattern=r"^(single|mirror|raid10|raidz|raidz2|raidz3|draid|draid2|draid3)$",
    )
    ashift: int = Field(
        default=12,
        description=(
            "create only: sector-size hint as power of 2 (12 = 4K, 13 = 8K "
            "NVMe). Cannot be changed after creation."
        ),
        ge=9, le=16,
    )
    compression: str = Field(
        default="lz4",
        description="create only: compression algorithm (lz4 recommended).",
        pattern=r"^(on|off|lzjb|lz4|zle|gzip|zstd)$",
    )
    add_storage: bool = Field(
        default=True,
        description="create only: also register the pool as PVE storage.",
    )
    cleanup_config: bool = Field(
        default=True,
        description="destroy only: also remove matching PVE storage entries.",
    )
    cleanup_disks: bool = Field(
        default=False,
        description=(
            "destroy only: also wipe the underlying disks. Extra dangerous."
        ),
    )
    confirm: bool = Field(default=False)
    i_understand_data_loss: bool = Field(
        default=False, description="Required for action='destroy'."
    )
    wait_seconds: int = Field(default=0, ge=0, le=600, description=WAIT_DESC)
    reason: Optional[str] = Field(default=None, max_length=200)

    @field_validator("devices")
    @classmethod
    def _validate_devices(cls, v):
        if v is None:
            return v
        for d in v:
            if not d.startswith("/dev/"):
                raise ValueError(f"Device must start with /dev/: {d}")
            if len(d) > 64 or any(c in d for c in " \t;|&'\""):
                raise ValueError(f"Invalid device path: {d}")
        return v

    @model_validator(mode="after")
    def _check_required(self):
        if self.action == "create" and not self.devices:
            raise ValueError("action='create' requires 'devices'.")
        return self


@mcp.tool(
    name="proxmox_zfs_pool_manage",
    annotations={
        "title": "Create / Destroy ZFS Pool",
        "readOnlyHint": False, "destructiveHint": True,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_zfs_pool_manage(params: ZfsPoolManageInput) -> str:
    """Create or destroy a ZFS pool.

    create: devices must be empty (wipe first via proxmox_disk_prepare);
    layout minimums — mirror≥2, raid10≥4 (even), raidz≥3, raidz2≥4, raidz3≥5.
    Requires confirm=true.

    destroy: all datasets, snapshots, and zvols are irretrievable. Requires
    BOTH confirm=true AND i_understand_data_loss=true.
    Set wait_seconds>0 for the task result inline.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm(f"proxmox_zfs_pool_manage (action={params.action})")

    if params.action == "destroy":
        if not params.i_understand_data_loss:
            return missing_data_loss_ack("proxmox_zfs_pool_manage (action=destroy)")
        query = {"cleanup-config": 1 if params.cleanup_config else 0,
                 "cleanup-disks": 1 if params.cleanup_disks else 0}
        try:
            task_id = await http_client.delete(
                f"/nodes/{params.node}/disks/zfs/{params.name}", params=query)
        except Exception as exc:
            return http_client.format_http_error(exc)
        suffix = await http_client.wait_for_task(
            params.node, task_id, params.wait_seconds)
        return (
            f"OK: Destroy of ZFS pool '{params.name}' started "
            f"({params.node}). Task: {task_id}.{suffix}"
        )

    # create
    min_devs = _RAID_MIN_DEVICES.get(params.raidlevel, 1)
    if len(params.devices) < min_devs:
        return (
            f"Error: raidlevel '{params.raidlevel}' requires at least {min_devs} "
            f"devices; got {len(params.devices)}."
        )
    if params.raidlevel == "raid10" and len(params.devices) % 2 != 0:
        return "Error: raid10 requires an even number of devices."

    payload = {
        "name": params.name,
        "devices": ",".join(params.devices),
        "raidlevel": params.raidlevel,
        "ashift": params.ashift,
        "compression": params.compression,
        "add_storage": 1 if params.add_storage else 0,
    }
    try:
        task_id = await http_client.post(
            f"/nodes/{params.node}/disks/zfs", data=payload)
    except Exception as exc:
        return http_client.format_http_error(exc)

    storage_msg = " Also registered as PVE storage." if params.add_storage else ""
    suffix = await http_client.wait_for_task(params.node, task_id, params.wait_seconds)
    if not suffix:
        suffix = " Use proxmox_get_zfs_pool to verify once the task completes."
    return (
        f"OK: ZFS pool '{params.name}' creation started on "
        f"{len(params.devices)} device(s), layout={params.raidlevel}, "
        f"ashift={params.ashift}, compression={params.compression} "
        f"({params.node}).{storage_msg} Task: {task_id}.{suffix}"
    )
