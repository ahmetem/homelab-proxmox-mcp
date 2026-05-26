"""Shared Pydantic input models."""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FormatInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'.",
    )


class NodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., description="Node name (e.g., 'pve')", min_length=1)
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'.",
    )


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


class VMActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., description="Node name", min_length=1)
    vmid: int = Field(..., description="VM or container ID", ge=100)
    vm_type: str = Field(
        default="qemu", description="VM type", pattern="^(qemu|lxc)$"
    )
    confirm: bool = Field(
        default=False,
        description="Must be true to execute. Only set after explicit user confirmation.",
    )
    reason: Optional[str] = Field(
        default=None, description="Optional note about why", max_length=200
    )


class SnapshotCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., min_length=1)
    vmid: int = Field(..., ge=100)
    vm_type: str = Field(default="qemu", pattern="^(qemu|lxc)$")
    snapname: str = Field(
        ...,
        description="Snapshot name (alphanumeric, dash, underscore)",
        min_length=1,
        max_length=40,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$",
    )
    description: Optional[str] = Field(default=None, max_length=200)
    confirm: bool = Field(default=False)


class SnapshotRollbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., min_length=1)
    vmid: int = Field(..., ge=100)
    vm_type: str = Field(default="qemu", pattern="^(qemu|lxc)$")
    snapname: str = Field(..., min_length=1, max_length=40)
    confirm: bool = Field(default=False)


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


class StorageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., min_length=1)
    storage: str = Field(default="local", description="Storage name")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)
