"""Shared Pydantic input models."""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


FIELDS_DESC = (
    "JSON mode only: return only these keys per object (e.g. "
    "['vmid','name','status']). Cuts output size drastically."
)

WAIT_DESC = (
    "Poll the started task up to this many seconds and report its final "
    "state inline (0 = return immediately with the task ID)."
)


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FormatInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'.",
    )
    fields: Optional[list[str]] = Field(default=None, description=FIELDS_DESC)


class NodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., description="Node name (e.g., 'pve')", min_length=1)
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'.",
    )
    fields: Optional[list[str]] = Field(default=None, description=FIELDS_DESC)


class VMInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., description="Node name (e.g., 'pve')", min_length=1)
    vmid: int = Field(..., description="VM or container ID", ge=100, le=999999999)
    vm_type: str = Field(
        default="qemu",
        description="VM type: 'qemu' for VMs, 'lxc' for containers",
        pattern="^(qemu|lxc)$",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'.",
    )
    fields: Optional[list[str]] = Field(default=None, description=FIELDS_DESC)


class VMListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: Optional[str] = Field(
        default=None, description="Only guests on this node.", max_length=64
    )
    vmid: Optional[int] = Field(
        default=None, description="Only this VMID.", ge=100, le=999999999
    )
    status: Optional[str] = Field(
        default=None,
        description="Only guests in this state.",
        pattern="^(running|stopped)$",
    )
    guest_type: Optional[str] = Field(
        default=None,
        description="Only 'qemu' VMs or 'lxc' containers.",
        pattern="^(qemu|lxc)$",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'.",
    )
    fields: Optional[list[str]] = Field(default=None, description=FIELDS_DESC)


class VMPowerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., description="Node name", min_length=1)
    vmid: int = Field(..., description="VM or container ID", ge=100)
    action: str = Field(
        ...,
        description=(
            "Power action: 'start', 'shutdown' (graceful ACPI), 'stop' "
            "(force, may lose data), 'reboot'."
        ),
        pattern="^(start|shutdown|stop|reboot)$",
    )
    vm_type: str = Field(
        default="qemu", description="VM type (auto-corrected)", pattern="^(qemu|lxc)$"
    )
    confirm: bool = Field(
        default=False,
        description="Must be true to execute. Only set after explicit user confirmation.",
    )
    wait_seconds: int = Field(default=0, ge=0, le=600, description=WAIT_DESC)
    reason: Optional[str] = Field(
        default=None, description="Optional note about why", max_length=200
    )


class SnapshotManageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(
        ...,
        description=(
            "'create' (confirm), 'rollback' (confirm; state after snapshot is "
            "lost), 'delete' (confirm + i_understand_data_loss)."
        ),
        pattern="^(create|rollback|delete)$",
    )
    node: str = Field(..., min_length=1)
    vmid: int = Field(..., ge=100)
    vm_type: str = Field(default="qemu", pattern="^(qemu|lxc)$")
    snapname: str = Field(
        ...,
        description="Snapshot name (letter first; alphanumeric, dash, underscore).",
        min_length=1,
        max_length=40,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$",
    )
    description: Optional[str] = Field(
        default=None, description="Only for action='create'.", max_length=200
    )
    force: bool = Field(
        default=False,
        description="Only for action='delete': force removal even with dangling references.",
    )
    confirm: bool = Field(default=False)
    i_understand_data_loss: bool = Field(
        default=False, description="Required for action='delete'."
    )
    wait_seconds: int = Field(default=0, ge=0, le=600, description=WAIT_DESC)
    reason: Optional[str] = Field(default=None, max_length=200)


class BackupListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., min_length=1)
    storage: str = Field(default="local", description="Storage name")
    vmid: Optional[int] = Field(
        default=None, description="Only backups of this VMID.", ge=100
    )
    limit: int = Field(
        default=50, description="Newest N backups to show.", ge=1, le=1000
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)
    fields: Optional[list[str]] = Field(default=None, description=FIELDS_DESC)


class BackupCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., min_length=1)
    vmid: int = Field(..., ge=100)
    storage: str = Field(default="local", description="Storage for backup")
    mode: str = Field(
        default="snapshot",
        description="Backup mode: snapshot, suspend, or stop",
        pattern="^(snapshot|suspend|stop)$",
    )
    compress: str = Field(
        default="zstd",
        description="Compression: none, lzo, gzip, zstd",
        pattern="^(none|lzo|gzip|zstd)$",
    )
    confirm: bool = Field(default=False)
    wait_seconds: int = Field(default=0, ge=0, le=600, description=WAIT_DESC)


class BackupRestoreInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(
        ..., description="Node to restore on (typically 'pve').", min_length=1
    )
    vmid: int = Field(
        ...,
        description=(
            "Target VMID for the restored guest. If a VM/CT with this ID "
            "already exists, restore is REFUSED unless force=true."
        ),
        ge=100,
        le=999999999,
    )
    archive: str = Field(
        ...,
        description=(
            "Backup archive volid as returned by proxmox_list_backups, e.g. "
            "'vmdata-backups:backup/vzdump-qemu-101-2026_05_23-19_03_50.vma.zst' "
            "or for PBS: 'wdmycloud-pbs:backup/vm/101/2026-05-25T03:00:00Z'."
        ),
        min_length=3,
        max_length=512,
    )
    vm_type: str = Field(
        default="qemu",
        description="Target type: 'qemu' for VM (.vma.*), 'lxc' for CT (.tar.*).",
        pattern="^(qemu|lxc)$",
    )
    storage: Optional[str] = Field(
        default=None,
        description=(
            "Override storage for the restored disks. If omitted, the "
            "original storage from the backup is used (which may not "
            "exist on this node)."
        ),
        max_length=64,
    )
    force: bool = Field(
        default=False,
        description=(
            "If true, allow overwriting an existing VM/CT with the same "
            "vmid. The existing guest is destroyed before restore. "
            "Requires i_understand_data_loss=true as well."
        ),
    )
    start_after_restore: bool = Field(
        default=False,
        description="Power on the guest as soon as the restore completes.",
    )
    confirm: bool = Field(default=False)
    i_understand_data_loss: bool = Field(
        default=False,
        description=(
            "Required when force=true (overwrites existing guest). The "
            "existing guest's disks are destroyed."
        ),
    )
    wait_seconds: int = Field(default=0, ge=0, le=600, description=WAIT_DESC)
    reason: Optional[str] = Field(default=None, max_length=200)


class VMResizeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., description="Node name", min_length=1)
    vmid: int = Field(..., description="VM or container ID", ge=100)
    vm_type: str = Field(
        default="qemu", description="VM type", pattern="^(qemu|lxc)$"
    )
    memory_mb: Optional[int] = Field(
        default=None,
        description="New RAM size in MB (e.g. 4096 for 4 GB). Omit to keep current.",
        ge=16,
        le=1048576,
    )
    cores: Optional[int] = Field(
        default=None,
        description="New CPU core count. Omit to keep current.",
        ge=1,
        le=256,
    )
    confirm: bool = Field(
        default=False,
        description="Must be true to execute. Only set after explicit user confirmation.",
    )
    reason: Optional[str] = Field(
        default=None, description="Optional note about why", max_length=200
    )
