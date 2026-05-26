"""Phase 3 SSH-backed ZFS tools: property read, pool status, scrub, send/recv.

Extends Phase 2.5 (ssh_zfs.py). Kept as a separate module so the original
file stays readable; reuses the same validators (_validate_dataset,
_validate_snapname) and the ssh.run_command / ssh.run_pipeline helpers.
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
)
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import ResponseFormat
from proxmox_mcp.tools.ssh_zfs import _validate_dataset, _validate_snapname


class ZfsGetPropertyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(
        ...,
        description="Dataset, snapshot, or pool name.",
        min_length=1, max_length=256,
    )
    property: str = Field(
        default="all",
        description=(
            "Property name to read, or 'all' for everything. Examples: "
            "'compressratio', 'used', 'recordsize', 'available'."
        ),
        min_length=1, max_length=64,
        pattern=r"^[a-z][a-z0-9:_-]*$",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        if "@" in v:
            ds, snap = v.split("@", 1)
            if _validate_dataset(ds) or _validate_snapname(snap):
                raise ValueError(f"Invalid snapshot name {v!r}")
            return v
        err = _validate_dataset(v)
        if err:
            raise ValueError(err)
        return v


class ZfsPoolStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pool: Optional[str] = Field(
        default=None,
        description="Pool name. Omit to show all pools.",
        max_length=64,
    )
    verbose: bool = Field(
        default=True,
        description="If true, `zpool status -v` (per-vdev errors). Else plain status.",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)

    @field_validator("pool")
    @classmethod
    def _v_pool(cls, v):
        if v is None:
            return v
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", v):
            raise ValueError("Invalid pool name.")
        return v


class ZfsScrubInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pool: str = Field(
        ...,
        description="Pool to scrub.",
        min_length=1, max_length=64,
        pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$",
    )
    stop: bool = Field(default=False, description="If true, send `zpool scrub -s` to stop an in-progress scrub.")
    confirm: bool = Field(default=False)
    reason: Optional[str] = Field(default=None, max_length=200)


class ZfsSendInput(BaseModel):
    """zfs send to a file on the Proxmox node, or piped to recv on the same node."""
    model_config = ConfigDict(extra="forbid")
    source: str = Field(
        ...,
        description=(
            "Source snapshot (dataset@snapname) or dataset to send. "
            "For datasets, ZFS will snapshot internally if -R is used."
        ),
        min_length=1, max_length=256,
    )
    target_file: Optional[str] = Field(
        default=None,
        description=(
            "Absolute file path on the Proxmox node to write the stream to. "
            "Must be inside /var/lib/vz, /tmp, or any ZFS-mounted dataset. "
            "Mutually exclusive with target_dataset."
        ),
        max_length=512,
        pattern=r"^/(var/lib/vz|tmp|mnt|nvmepool|vmdata)/[A-Za-z0-9._/-]+$",
    )
    target_dataset: Optional[str] = Field(
        default=None,
        description=(
            "Local ZFS dataset to receive into via `zfs send … | zfs recv`. "
            "Useful for fast pool-to-pool copy on the same node. Mutually "
            "exclusive with target_file."
        ),
        max_length=256,
    )
    incremental_from: Optional[str] = Field(
        default=None,
        description=(
            "Earlier snapshot name (just the @suffix or full path) for "
            "incremental send (zfs send -i)."
        ),
        max_length=256,
    )
    replication: bool = Field(
        default=False,
        description=(
            "If true, use -R (replication stream: include all descendants, "
            "snapshots, and properties)."
        ),
    )
    raw: bool = Field(
        default=False,
        description=(
            "If true, use -w (raw — preserves encryption/compression bits "
            "without decompressing)."
        ),
    )
    confirm: bool = Field(default=False)
    reason: Optional[str] = Field(default=None, max_length=200)

    @field_validator("source")
    @classmethod
    def _v_source(cls, v: str) -> str:
        if "@" in v:
            ds, snap = v.split("@", 1)
            if _validate_dataset(ds) or _validate_snapname(snap):
                raise ValueError(f"Invalid source: {v!r}")
        else:
            err = _validate_dataset(v)
            if err:
                raise ValueError(err)
        return v

    @field_validator("target_dataset")
    @classmethod
    def _v_target_ds(cls, v):
        if v is None:
            return v
        err = _validate_dataset(v)
        if err:
            raise ValueError(err)
        return v

    @field_validator("incremental_from")
    @classmethod
    def _v_inc(cls, v):
        if v is None:
            return v
        if v.startswith("@"):
            err = _validate_snapname(v[1:])
            if err:
                raise ValueError(err)
        elif "@" in v:
            ds, snap = v.split("@", 1)
            if _validate_dataset(ds) or _validate_snapname(snap):
                raise ValueError(f"Invalid incremental_from: {v!r}")
        else:
            raise ValueError("incremental_from must be '@snapname' or 'dataset@snapname'.")
        return v


@mcp.tool(
    name="proxmox_zfs_get_property",
    annotations={
        "title": "Get ZFS Property",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def proxmox_zfs_get_property(params: ZfsGetPropertyInput) -> str:
    """Read one or all properties of a dataset, snapshot, or pool.

    Examples:
      - name='nvmepool', property='compressratio'
      - name='nvmepool/test', property='all'
      - name='nvmepool@daily', property='used'
    """
    cfg_err = require_ssh()
    if cfg_err:
        return cfg_err

    argv = ["zfs", "get", "-H", "-p", "-o", "property,value,source",
            params.property, params.name]
    try:
        rc, out, err = await ssh.run_command(argv)
    except ssh.SshError as exc:
        return ssh.format_ssh_error(exc)
    if rc != 0:
        return f"Error: zfs get failed (rc={rc}).\nstderr: {err.strip()}"

    rows = [line.split("\t") for line in out.strip().splitlines() if line.strip()]

    if params.response_format == ResponseFormat.JSON:
        keys = ["property", "value", "source"]
        return json.dumps([dict(zip(keys, r)) for r in rows if len(r) >= 3], indent=2)

    if not rows:
        return f"_No properties returned for `{params.name}`._"

    lines = [
        f"## ZFS properties — `{params.name}`",
        "",
        "| Property | Value | Source |",
        "| --- | --- | --- |",
    ]
    for r in rows:
        if len(r) < 3:
            continue
        prop, val, src = r[:3]
        lines.append(f"| `{prop}` | `{val}` | {src} |")
    return "\n".join(lines)


@mcp.tool(
    name="proxmox_zfs_pool_status",
    annotations={
        "title": "Get ZFS Pool Status (zpool status)",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def proxmox_zfs_pool_status(params: ZfsPoolStatusInput) -> str:
    """Show `zpool status [-v]` output: vdev tree, scrub progress, errors.

    With verbose=true (default), per-vdev R/W/CK error counters are shown.
    Use this to detect impending disk failures or check scrub progress.
    """
    cfg_err = require_ssh()
    if cfg_err:
        return cfg_err

    argv = ["zpool", "status"]
    if params.verbose:
        argv.append("-v")
    if params.pool:
        argv.append(params.pool)

    try:
        rc, out, err = await ssh.run_command(argv)
    except ssh.SshError as exc:
        return ssh.format_ssh_error(exc)
    if rc != 0:
        return f"Error: zpool status failed (rc={rc}).\nstderr: {err.strip()}"

    if params.response_format == ResponseFormat.JSON:
        return json.dumps({"output": out}, indent=2)

    text = out.strip() or "(no pools)"
    health_line = ""
    for line in text.splitlines():
        if line.strip().lower().startswith("state:"):
            state = line.split(":", 1)[1].strip()
            health_line = f"\n**Health**: {health_icon(state)} {state}\n"
            break

    title = f"## zpool status `{params.pool}`" if params.pool else "## zpool status (all pools)"
    return f"{title}\n{health_line}\n```\n{text}\n```"


@mcp.tool(
    name="proxmox_zfs_scrub",
    annotations={
        "title": "Start or Stop ZFS Scrub",
        "readOnlyHint": False, "destructiveHint": False,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_zfs_scrub(params: ZfsScrubInput) -> str:
    """Start (or stop) a ZFS scrub on a pool.

    Scrub reads every block and verifies checksums — the safe way to detect
    silent data corruption. Scrubs run in the background and may take hours
    on large pools. Use proxmox_zfs_pool_status to monitor progress.

    Set stop=true to abort an in-progress scrub.

    Requires confirm=true.
    """
    cfg_err = require_ssh()
    if cfg_err:
        return cfg_err
    if not params.confirm:
        return missing_confirm("proxmox_zfs_scrub")

    argv = ["zpool", "scrub"]
    if params.stop:
        argv.append("-s")
    argv.append(params.pool)

    try:
        rc, out, err = await ssh.run_command(argv)
    except ssh.SshError as exc:
        return ssh.format_ssh_error(exc)
    if rc != 0:
        return f"Error: zpool scrub failed (rc={rc}).\nstderr: {err.strip()}"

    action = "stop requested" if params.stop else "started"
    return (
        f"OK: Scrub {action} on pool `{params.pool}`. "
        "Monitor with proxmox_zfs_pool_status."
    )


@mcp.tool(
    name="proxmox_zfs_send",
    annotations={
        "title": "ZFS Send (replicate snapshot to file or local dataset)",
        "readOnlyHint": False, "destructiveHint": False,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_zfs_send(params: ZfsSendInput) -> str:
    """Stream a ZFS snapshot to a file or pipe into a local zfs recv.

    Two modes, exactly one must be chosen:

    A) target_file=/path/on/node — writes the stream to a file. Useful for:
       - one-shot backups you'll later transport elsewhere
       - dumps into a different filesystem (e.g. NFS mount)

    B) target_dataset=poolB/path — pipes `zfs send | zfs recv` on the same
       node. Useful for fast pool-to-pool copy (e.g. move data from vmdata
       HDD pool to nvmepool NVMe pool while preserving snapshots and
       properties).

    Flags:
      - replication=true (-R): include descendants, snapshots, properties.
      - raw=true (-w): preserve encrypted/compressed form without decoding.
      - incremental_from='@snap' or 'ds@snap' (-i): incremental from an
        earlier snapshot — only the delta is sent.

    Cross-node send (to a different host via SSH) is NOT in this tool to
    keep the SSH boundary clear; for that, run `zfs send | ssh host zfs recv`
    manually from the source node.

    Requires confirm=true.
    """
    cfg_err = require_ssh()
    if cfg_err:
        return cfg_err
    if not params.confirm:
        return missing_confirm("proxmox_zfs_send")

    if bool(params.target_file) == bool(params.target_dataset):
        return "Error: Specify exactly one of target_file or target_dataset."

    send_argv = ["zfs", "send"]
    if params.replication:
        send_argv.append("-R")
    if params.raw:
        send_argv.append("-w")
    if params.incremental_from:
        inc = params.incremental_from
        if inc.startswith("@"):
            if "@" not in params.source:
                return (
                    "Error: incremental_from='@name' requires source to be "
                    "a snapshot (dataset@name)."
                )
            src_ds = params.source.split("@", 1)[0]
            inc = f"{src_ds}{inc}"
        send_argv.extend(["-i", inc])
    send_argv.append(params.source)

    if params.target_file:
        dd_argv = ["dd", f"of={params.target_file}", "bs=1M", "status=none"]
        try:
            rc, out, err = await ssh.run_pipeline([send_argv, dd_argv])
        except ssh.SshError as exc:
            return ssh.format_ssh_error(exc)
        if rc != 0:
            return (
                f"Error: zfs send to file failed (rc={rc}).\n"
                f"stderr: {err.strip()}"
            )
        return (
            f"OK: Sent `{params.source}` → file `{params.target_file}` "
            f"(replication={params.replication}, raw={params.raw}, "
            f"incremental={'yes' if params.incremental_from else 'no'})."
        )

    recv_argv = ["zfs", "recv", "-F", params.target_dataset]
    try:
        rc, out, err = await ssh.run_pipeline([send_argv, recv_argv], timeout=None)
    except ssh.SshError as exc:
        return ssh.format_ssh_error(exc)

    if rc != 0:
        return (
            f"Error: zfs send|recv pipeline failed (rc={rc}).\n"
            f"stderr: {err.strip()}"
        )
    return (
        f"OK: Replicated `{params.source}` → `{params.target_dataset}` "
        f"(replication={params.replication}, raw={params.raw}, "
        f"incremental={'yes' if params.incremental_from else 'no'})."
    )
