"""Phase 2: ZFS pool create / destroy.

Backed by Proxmox REST endpoints:
  - POST   /nodes/{node}/disks/zfs        body: name, devices, raidlevel,
                                                ashift, compression, add_storage
  - DELETE /nodes/{node}/disks/zfs/{name} query: cleanup-config, cleanup-disks

raidlevel values accepted by Proxmox 8.x / 9.x:
  single, mirror, raid10, raidz, raidz2, raidz3, draid, draid2, draid3

Minimum devices per layout:
  single:  1
  mirror:  2 (multiple mirror vdevs use 2N devices total)
  raid10:  4, must be even
  raidz:   3
  raidz2:  4
  raidz3:  5

compression: on, off, lzjb, lz4 (default), zle, gzip, zstd
ashift: 9..16 (default 12 = 4K sectors; use 13 for many modern NVMe)
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import missing_confirm, missing_data_loss_ack
from proxmox_mcp.mcp_instance import mcp


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


class ZfsCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., min_length=1)
    name: str = Field(
        ..., description="ZFS pool name (must start with letter).",
        min_length=1, max_length=64, pattern=_POOL_NAME,
    )
    devices: list[str] = Field(
        ...,
        description=(
            "Block devices to add to the pool. All must be empty — wipe first "
            "if needed. Single-disk pools use one device; mirror/raidz use multiple."
        ),
        min_length=1, max_length=64,
    )
    raidlevel: str = Field(
        default="single",
        description=(
            "Pool layout: single, mirror, raid10, raidz, raidz2, raidz3, "
            "draid, draid2, draid3. Defaults to 'single' (one-disk pool)."
        ),
        pattern=r"^(single|mirror|raid10|raidz|raidz2|raidz3|draid|draid2|draid3)$",
    )
    ashift: int = Field(
        default=12,
        description=(
            "ZFS ashift (sector-size hint as power of 2). 12 = 4K (most disks), "
            "13 = 8K (many newer NVMe). Cannot be changed after creation."
        ),
        ge=9, le=16,
    )
    compression: str = Field(
        default="lz4",
        description="Compression algorithm. lz4 is recommended (fast, ubiquitous).",
        pattern=r"^(on|off|lzjb|lz4|zle|gzip|zstd)$",
    )
    add_storage: bool = Field(
        default=True,
        description="If true (default), also register the pool as PVE storage.",
    )
    confirm: bool = Field(default=False)
    reason: Optional[str] = Field(default=None, max_length=200)

    @field_validator("devices")
    @classmethod
    def _validate_devices(cls, v: list[str]) -> list[str]:
        for d in v:
            if not d.startswith("/dev/"):
                raise ValueError(f"Device must start with /dev/: {d}")
            if len(d) > 64 or any(c in d for c in " \t;|&'\""):
                raise ValueError(f"Invalid device path: {d}")
        return v


class ZfsDestroyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., min_length=1)
    name: str = Field(
        ..., description="ZFS pool name to destroy.",
        min_length=1, max_length=64, pattern=_POOL_NAME,
    )
    cleanup_config: bool = Field(default=True, description="If true, also remove matching PVE storage configuration entries.")
    cleanup_disks: bool = Field(
        default=False,
        description=(
            "If true, also wipe the underlying disks after exporting the pool. "
            "Extra dangerous — set only when you actually want to free the disks."
        ),
    )
    confirm: bool = Field(default=False)
    i_understand_data_loss: bool = Field(default=False)
    reason: Optional[str] = Field(default=None, max_length=200)


@mcp.tool(
    name="proxmox_create_zfs_pool",
    annotations={
        "title": "Create ZFS Pool",
        "readOnlyHint": False, "destructiveHint": True,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_create_zfs_pool(params: ZfsCreateInput) -> str:
    """Create a ZFS pool on one or more devices, optionally registering it as PVE storage.

    All devices must be empty (no partition table, no FS signatures). Use
    proxmox_wipe_disk first if needed.

    Layout choices:
      - single   : one disk, no redundancy
      - mirror   : N-way mirror (every disk = full copy)
      - raid10   : striped mirrors (even number of disks, ≥4)
      - raidz    : single parity, ≥3 disks
      - raidz2   : double parity, ≥4 disks
      - raidz3   : triple parity, ≥5 disks

    Requires confirm=true.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_create_zfs_pool")

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
        task_id = await http_client.post(f"/nodes/{params.node}/disks/zfs", data=payload)
    except Exception as exc:
        return http_client.format_http_error(exc)

    storage_msg = " Also registered as PVE storage." if params.add_storage else ""
    return (
        f"OK: ZFS pool '{params.name}' creation started on "
        f"{len(params.devices)} device(s), layout={params.raidlevel}, "
        f"ashift={params.ashift}, compression={params.compression} "
        f"({params.node}).{storage_msg} Task: {task_id}. "
        "Use proxmox_get_zfs_pool to verify once the task completes."
    )


@mcp.tool(
    name="proxmox_destroy_zfs_pool",
    annotations={
        "title": "Destroy ZFS Pool (DESTROY ALL DATA)",
        "readOnlyHint": False, "destructiveHint": True,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_destroy_zfs_pool(params: ZfsDestroyInput) -> str:
    """Destroy a ZFS pool and all data within it.

    Requires BOTH confirm=true AND i_understand_data_loss=true.

    All datasets, snapshots, and zvols are irretrievable. If cleanup_disks=true,
    the underlying disks are also wiped.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_destroy_zfs_pool")
    if not params.i_understand_data_loss:
        return missing_data_loss_ack("proxmox_destroy_zfs_pool")

    query = {"cleanup-config": 1 if params.cleanup_config else 0, "cleanup-disks": 1 if params.cleanup_disks else 0}
    try:
        task_id = await http_client.delete(f"/nodes/{params.node}/disks/zfs/{params.name}", params=query)
    except Exception as exc:
        return http_client.format_http_error(exc)

    return f"OK: Destroy of ZFS pool '{params.name}' started ({params.node}). Task: {task_id}"
