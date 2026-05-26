"""SSH-backed ZFS dataset / property / snapshot tools.

The Proxmox REST API does not expose `zfs create`, `zfs destroy`, `zfs set`,
or `zfs snapshot` for arbitrary datasets. These tools fill that gap.

Naming rules enforced here for safety:
- Dataset paths: `<pool>/<path>` with each segment matching [A-Za-z0-9][A-Za-z0-9_.-]*
- Snapshot suffix: same character set
- No spaces, semicolons, ampersands, backticks, etc.

All destructive operations require confirm=true; recursive destroy also
requires i_understand_data_loss=true.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from proxmox_mcp import ssh
from proxmox_mcp.config import require_ssh
from proxmox_mcp.format import (
    fmt_bytes,
    health_icon,
    missing_confirm,
    missing_data_loss_ack,
)
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import ResponseFormat


_SEG = r"[A-Za-z0-9][A-Za-z0-9_.:-]*"
_DATASET_RE = re.compile(rf"^{_SEG}(/{_SEG})*$")
_SNAPNAME_RE = re.compile(rf"^{_SEG}$")

ALLOWED_ZFS_PROPS = frozenset(
    {
        "compression", "atime", "recordsize", "volblocksize",
        "quota", "refquota", "reservation", "refreservation",
        "sync", "logbias", "primarycache", "secondarycache",
        "dedup", "snapdir", "checksum", "copies",
        "xattr", "acltype", "exec", "setuid", "readonly",
        "mountpoint", "canmount",
    }
)

_PROP_VALUE_RE = re.compile(r"^[A-Za-z0-9._%/=:-]+$")


def _validate_dataset(name: str) -> Optional[str]:
    if not name or len(name) > 256:
        return "Dataset name must be 1-256 chars."
    if not _DATASET_RE.fullmatch(name):
        return (
            f"Invalid dataset path {name!r}. Allowed: pool/segment[/segment...]"
            " where each segment is [A-Za-z0-9][A-Za-z0-9_.:-]*"
        )
    return None


def _validate_snapname(name: str) -> Optional[str]:
    if not name or len(name) > 64:
        return "Snapshot name must be 1-64 chars."
    if not _SNAPNAME_RE.fullmatch(name):
        return f"Invalid snapshot name {name!r} (allowed: [A-Za-z0-9][A-Za-z0-9_.:-]*)."
    return None


class ZfsCreateDatasetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(
        ...,
        description="Full dataset path, e.g. 'nvmepool/data' or 'nvmepool/vm/lxc'.",
        min_length=1, max_length=256,
    )
    parents: bool = Field(default=False, description="Create missing parent datasets (zfs create -p).")
    properties: Optional[dict[str, str]] = Field(
        default=None,
        description=(
            "Optional ZFS properties to set at creation, e.g. "
            "{'compression': 'lz4', 'atime': 'off'}."
        ),
    )
    confirm: bool = Field(default=False)
    reason: Optional[str] = Field(default=None, max_length=200)

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        err = _validate_dataset(v)
        if err:
            raise ValueError(err)
        return v

    @field_validator("properties")
    @classmethod
    def _v_props(cls, v):
        if v is None:
            return v
        if len(v) > 32:
            raise ValueError("Too many properties (max 32).")
        for k, val in v.items():
            if k not in ALLOWED_ZFS_PROPS:
                raise ValueError(
                    f"Property {k!r} is not allow-listed. Allowed: "
                    f"{sorted(ALLOWED_ZFS_PROPS)}"
                )
            if not isinstance(val, str) or not _PROP_VALUE_RE.fullmatch(val):
                raise ValueError(f"Property value for {k!r} contains disallowed characters.")
        return v


class ZfsDestroyDatasetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=256)
    recursive: bool = Field(default=False, description="Destroy children and snapshots too (zfs destroy -r).")
    confirm: bool = Field(default=False)
    i_understand_data_loss: bool = Field(default=False)
    reason: Optional[str] = Field(default=None, max_length=200)

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        err = _validate_dataset(v)
        if err:
            raise ValueError(err)
        return v


class ZfsSetPropertyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=256)
    property: str = Field(..., min_length=1, max_length=64)
    value: str = Field(..., min_length=1, max_length=128)
    confirm: bool = Field(default=False)
    reason: Optional[str] = Field(default=None, max_length=200)

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        err = _validate_dataset(v)
        if err:
            raise ValueError(err)
        return v

    @field_validator("property")
    @classmethod
    def _v_prop(cls, v: str) -> str:
        if v not in ALLOWED_ZFS_PROPS:
            raise ValueError(
                f"Property {v!r} is not allow-listed. "
                f"Allowed: {sorted(ALLOWED_ZFS_PROPS)}"
            )
        return v

    @field_validator("value")
    @classmethod
    def _v_val(cls, v: str) -> str:
        if not _PROP_VALUE_RE.fullmatch(v):
            raise ValueError("value contains disallowed characters.")
        return v


class ZfsSnapshotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset: str = Field(..., min_length=1, max_length=256)
    snapname: str = Field(..., min_length=1, max_length=64)
    recursive: bool = Field(default=False, description="Snapshot the dataset and all descendants atomically.")
    confirm: bool = Field(default=False)
    reason: Optional[str] = Field(default=None, max_length=200)

    @field_validator("dataset")
    @classmethod
    def _v_dataset(cls, v: str) -> str:
        err = _validate_dataset(v)
        if err:
            raise ValueError(err)
        return v

    @field_validator("snapname")
    @classmethod
    def _v_snap(cls, v: str) -> str:
        err = _validate_snapname(v)
        if err:
            raise ValueError(err)
        return v


class ZfsListDatasetsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pool: Optional[str] = Field(
        default=None,
        description="If set, restrict listing to this pool. Otherwise list all.",
        max_length=64,
    )
    include_snapshots: bool = Field(default=False)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)

    @field_validator("pool")
    @classmethod
    def _v_pool(cls, v):
        if v is None:
            return v
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", v):
            raise ValueError("Invalid pool name.")
        return v


@mcp.tool(
    name="proxmox_zfs_create_dataset",
    annotations={
        "title": "Create ZFS Dataset",
        "readOnlyHint": False, "destructiveHint": False,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_zfs_create_dataset(params: ZfsCreateDatasetInput) -> str:
    """Create a new ZFS dataset (filesystem) via SSH.

    Examples:
      - name='nvmepool/data', properties={'compression': 'zstd'}
      - name='nvmepool/vms/web01', parents=true

    Requires confirm=true.
    """
    cfg_err = require_ssh()
    if cfg_err:
        return cfg_err
    if not params.confirm:
        return missing_confirm("proxmox_zfs_create_dataset")

    argv: list[str] = ["zfs", "create"]
    if params.parents:
        argv.append("-p")
    if params.properties:
        for k, v in params.properties.items():
            argv.extend(["-o", f"{k}={v}"])
    argv.append(params.name)

    try:
        rc, out, err = await ssh.run_command(argv)
    except ssh.SshError as exc:
        return ssh.format_ssh_error(exc)

    if rc != 0:
        return f"Error: zfs create failed (rc={rc}).\nstderr: {err.strip()}"
    return f"OK: Dataset `{params.name}` created."


@mcp.tool(
    name="proxmox_zfs_destroy_dataset",
    annotations={
        "title": "Destroy ZFS Dataset (DESTROY DATA)",
        "readOnlyHint": False, "destructiveHint": True,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_zfs_destroy_dataset(params: ZfsDestroyDatasetInput) -> str:
    """Destroy a ZFS dataset (and optionally its children/snapshots).

    Requires confirm=true. With recursive=true, also requires
    i_understand_data_loss=true because children + all snapshots will be lost.
    """
    cfg_err = require_ssh()
    if cfg_err:
        return cfg_err
    if not params.confirm:
        return missing_confirm("proxmox_zfs_destroy_dataset")
    if params.recursive and not params.i_understand_data_loss:
        return missing_data_loss_ack("proxmox_zfs_destroy_dataset")

    argv = ["zfs", "destroy"]
    if params.recursive:
        argv.append("-r")
    argv.append(params.name)

    try:
        rc, out, err = await ssh.run_command(argv)
    except ssh.SshError as exc:
        return ssh.format_ssh_error(exc)

    if rc != 0:
        return f"Error: zfs destroy failed (rc={rc}).\nstderr: {err.strip()}"
    return f"OK: Dataset `{params.name}` destroyed."


@mcp.tool(
    name="proxmox_zfs_set_property",
    annotations={
        "title": "Set ZFS Property",
        "readOnlyHint": False, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def proxmox_zfs_set_property(params: ZfsSetPropertyInput) -> str:
    """Set a ZFS property on a dataset (zfs set property=value name).

    Only allow-listed properties are accepted; the value must match a strict
    character set (alphanumerics, dot, dash, slash, percent, equals, colon).

    Requires confirm=true.
    """
    cfg_err = require_ssh()
    if cfg_err:
        return cfg_err
    if not params.confirm:
        return missing_confirm("proxmox_zfs_set_property")

    argv = ["zfs", "set", f"{params.property}={params.value}", params.name]
    try:
        rc, out, err = await ssh.run_command(argv)
    except ssh.SshError as exc:
        return ssh.format_ssh_error(exc)
    if rc != 0:
        return f"Error: zfs set failed (rc={rc}).\nstderr: {err.strip()}"
    return f"OK: Set `{params.property}={params.value}` on `{params.name}`."


@mcp.tool(
    name="proxmox_zfs_create_snapshot",
    annotations={
        "title": "Create ZFS Snapshot",
        "readOnlyHint": False, "destructiveHint": False,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_zfs_create_snapshot(params: ZfsSnapshotInput) -> str:
    """Create a ZFS snapshot: zfs snapshot [-r] dataset@snapname.

    Snapshots are instant and effectively free to create. Use recursive=true
    to snapshot the dataset and all its children atomically.

    Requires confirm=true.
    """
    cfg_err = require_ssh()
    if cfg_err:
        return cfg_err
    if not params.confirm:
        return missing_confirm("proxmox_zfs_create_snapshot")

    argv = ["zfs", "snapshot"]
    if params.recursive:
        argv.append("-r")
    argv.append(f"{params.dataset}@{params.snapname}")

    try:
        rc, out, err = await ssh.run_command(argv)
    except ssh.SshError as exc:
        return ssh.format_ssh_error(exc)
    if rc != 0:
        return f"Error: zfs snapshot failed (rc={rc}).\nstderr: {err.strip()}"
    return f"OK: Snapshot `{params.dataset}@{params.snapname}` created."


@mcp.tool(
    name="proxmox_zfs_list_datasets",
    annotations={
        "title": "List ZFS Datasets",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def proxmox_zfs_list_datasets(params: ZfsListDatasetsInput) -> str:
    """List ZFS datasets via SSH (`zfs list -H -p -o name,type,used,avail,refer,mountpoint`).

    Optional: scope to a pool, include snapshots.
    """
    cfg_err = require_ssh()
    if cfg_err:
        return cfg_err

    argv = [
        "zfs", "list",
        "-H", "-p",
        "-o", "name,type,used,avail,refer,mountpoint",
    ]
    if params.include_snapshots:
        argv.extend(["-t", "filesystem,volume,snapshot"])
    else:
        argv.extend(["-t", "filesystem,volume"])
    if params.pool:
        argv.extend(["-r", params.pool])

    try:
        rc, out, err = await ssh.run_command(argv)
    except ssh.SshError as exc:
        return ssh.format_ssh_error(exc)
    if rc != 0:
        return f"Error: zfs list failed (rc={rc}).\nstderr: {err.strip()}"

    rows = [line.split("\t") for line in out.strip().splitlines() if line.strip()]

    if params.response_format == ResponseFormat.JSON:
        keys = ["name", "type", "used", "avail", "refer", "mountpoint"]
        return json.dumps([dict(zip(keys, r)) for r in rows], indent=2)

    if not rows:
        scope = f" under `{params.pool}`" if params.pool else ""
        return f"_No datasets found{scope}._"

    lines = [
        "## ZFS datasets",
        "",
        "| Name | Type | Used | Avail | Refer | Mountpoint |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        if len(r) < 6:
            continue
        name, dtype, used, avail, refer, mp = r[:6]
        lines.append(
            f"| `{name}` | {dtype} | {fmt_bytes(used)} | "
            f"{fmt_bytes(avail) if avail != '-' else '-'} | "
            f"{fmt_bytes(refer)} | `{mp}` |"
        )
    return "\n".join(lines)
