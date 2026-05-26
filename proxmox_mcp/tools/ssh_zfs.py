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

import fnmatch
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
# Pattern for snapshot glob matching: snapshot-name chars plus * and ?
# Disallows spaces, shell metacharacters, brace expansion, etc.
_SNAP_PATTERN_RE = re.compile(r"^[A-Za-z0-9_.:*?-]+$")
# Whitelist of zfs properties we allow setting. Lock down by default; add as needed.
ALLOWED_ZFS_PROPS = frozenset(
    {
        "compression",
        "atime",
        "recordsize",
        "volblocksize",
        "quota",
        "refquota",
        "reservation",
        "refreservation",
        "sync",
        "logbias",
        "primarycache",
        "secondarycache",
        "dedup",
        "snapdir",
        "checksum",
        "copies",
        "xattr",
        "acltype",
        "exec",
        "setuid",
        "readonly",
        "mountpoint",
        "canmount",
    }
)

# Value pattern: alphanumerics, dot, percent, slash, dash, underscore, equals.
# Disallows spaces, semicolons, quotes, backticks, etc.
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
        min_length=1,
        max_length=256,
    )
    parents: bool = Field(
        default=False,
        description="Create missing parent datasets (zfs create -p).",
    )
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
                raise ValueError(
                    f"Property value for {k!r} contains disallowed characters."
                )
        return v


class ZfsDestroyDatasetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=256)
    recursive: bool = Field(
        default=False,
        description="Destroy children and snapshots too (zfs destroy -r).",
    )
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
    recursive: bool = Field(
        default=False,
        description="Snapshot the dataset and all descendants atomically.",
    )
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


class ZfsDestroySnapshotsByPatternInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset: str = Field(
        ...,
        description=(
            "Dataset whose snapshots will be filtered, e.g. "
            "'nvmepool/vm-101-disk-0'. Snapshots of CHILD datasets are "
            "NOT included unless recursive=true."
        ),
        min_length=1,
        max_length=256,
    )
    pattern: str = Field(
        ...,
        description=(
            "Glob pattern matched against the snapshot name (the part after "
            "'@'). Supports * and ? only. Examples: 'autosnap_*', "
            "'pre_test_*', '*_daily_*'. No brace expansion, no character "
            "classes."
        ),
        min_length=1,
        max_length=64,
    )
    recursive: bool = Field(
        default=False,
        description=(
            "If true, also match snapshots of descendant datasets "
            "(zfs list -r)."
        ),
    )
    dry_run: bool = Field(
        default=True,
        description=(
            "When true (default), only LIST matching snapshots without "
            "deleting anything. Set to false to actually delete. The "
            "default-true is intentional: pattern bugs that match too many "
            "snapshots are the obvious risk here."
        ),
    )
    max_delete: int = Field(
        default=1000,
        description=(
            "Hard upper bound on how many snapshots a single call will "
            "delete. If the match set is larger, the call refuses and "
            "asks you to narrow the pattern or raise this limit explicitly."
        ),
        ge=1,
        le=10_000,
    )
    confirm: bool = Field(
        default=False,
        description="Required when dry_run=false.",
    )
    i_understand_data_loss: bool = Field(
        default=False,
        description=(
            "Required when dry_run=false. Bulk snapshot deletion is "
            "irreversible; the data unique to those snapshots is gone."
        ),
    )
    reason: Optional[str] = Field(default=None, max_length=200)

    @field_validator("dataset")
    @classmethod
    def _v_dataset(cls, v: str) -> str:
        err = _validate_dataset(v)
        if err:
            raise ValueError(err)
        return v

    @field_validator("pattern")
    @classmethod
    def _v_pattern(cls, v: str) -> str:
        if not _SNAP_PATTERN_RE.fullmatch(v):
            raise ValueError(
                f"Invalid pattern {v!r}. Allowed: alphanumerics plus "
                "_ . : - * ? (glob wildcards only — no brace expansion)."
            )
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
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
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
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
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
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
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
    return (
        f"OK: Set `{params.property}={params.value}` on `{params.name}`."
    )


@mcp.tool(
    name="proxmox_zfs_create_snapshot",
    annotations={
        "title": "Create ZFS Snapshot",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
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
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
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
        "-H",  # no header, tab-separated
        "-p",  # parsable (raw bytes)
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


@mcp.tool(
    name="proxmox_zfs_destroy_snapshots_by_pattern",
    annotations={
        "title": "Destroy ZFS Snapshots Matching a Glob Pattern",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def proxmox_zfs_destroy_snapshots_by_pattern(
    params: ZfsDestroySnapshotsByPatternInput,
) -> str:
    """Bulk-delete ZFS snapshots whose name matches a glob pattern.

    Intended use case: sanoid / zfs-auto-snapshot retention catch-up,
    cleaning up `pre_*` test snapshots, removing day-old hourlies after
    a successful upgrade, etc. NOT a replacement for sanoid's own
    retention policy — this is the manual-cleanup escape hatch.

    Two-step by design:
      1. dry_run=true (default): list every snapshot that WOULD be
         deleted. Always run this first.
      2. dry_run=false + confirm=true + i_understand_data_loss=true:
         actually delete.

    Safety rails:
      - Pattern is restricted to glob wildcards (* and ?), no shell
        metacharacters
      - Only matches against the snapshot name (the part after '@'),
        not the dataset path
      - max_delete caps a single call's deletions (default 1000)
      - Each snapshot deleted with its own `zfs destroy` — atomicity
        is per-snapshot, not per-batch. Partial completion is logged.

    Returns:
        str: For dry_run: the list of matching snapshots with creation
             time and `used` size. For real run: per-snapshot result
             and a summary.
    """
    cfg_err = require_ssh()
    if cfg_err:
        return cfg_err

    # Step 1: list snapshots of the dataset (and descendants if recursive).
    list_argv = [
        "zfs", "list",
        "-H",  # no header
        "-p",  # parsable bytes
        "-t", "snapshot",
        "-o", "name,creation,used",
    ]
    if params.recursive:
        list_argv.append("-r")
    list_argv.append(params.dataset)

    try:
        rc, out, err = await ssh.run_command(list_argv)
    except ssh.SshError as exc:
        return ssh.format_ssh_error(exc)

    if rc != 0:
        return (
            f"Error: zfs list failed (rc={rc}).\nstderr: {err.strip()}\n"
            "Hint: does the dataset exist? Check with proxmox_zfs_list_datasets."
        )

    matches: list[tuple[str, str, str]] = []  # (full_name, creation_epoch, used_bytes)
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        full_name = parts[0]  # e.g. "nvmepool/vm-101-disk-0@autosnap_2026-05-20"
        if "@" not in full_name:
            continue
        _, snapname = full_name.split("@", 1)
        if fnmatch.fnmatchcase(snapname, params.pattern):
            matches.append((full_name, parts[1], parts[2]))

    if not matches:
        return (
            f"_No snapshots of `{params.dataset}` match pattern "
            f"`{params.pattern}`{' (recursive)' if params.recursive else ''}._"
        )

    # Format the match list once — same body for dry_run and real run.
    import datetime as _dt
    table = [
        "| Snapshot | Created | Used |",
        "| --- | --- | --- |",
    ]
    for name, creation, used in matches:
        try:
            ts = _dt.datetime.fromtimestamp(int(creation)).strftime(
                "%Y-%m-%d %H:%M"
            )
        except (ValueError, TypeError):
            ts = creation
        table.append(f"| `{name}` | {ts} | {fmt_bytes(used)} |")

    # Dry run: report and stop.
    if params.dry_run:
        return (
            f"## Dry run: {len(matches)} snapshot(s) match\n\n"
            f"Dataset: `{params.dataset}`"
            f"{' (recursive)' if params.recursive else ''}  \n"
            f"Pattern: `{params.pattern}`\n\n"
            + "\n".join(table)
            + "\n\n"
            "To actually delete: re-run with dry_run=false, confirm=true, "
            "i_understand_data_loss=true."
        )

    # Real run: full gate.
    if not params.confirm:
        return missing_confirm("proxmox_zfs_destroy_snapshots_by_pattern")
    if not params.i_understand_data_loss:
        return missing_data_loss_ack("proxmox_zfs_destroy_snapshots_by_pattern")
    if len(matches) > params.max_delete:
        return (
            f"Refused: pattern matched {len(matches)} snapshots, exceeds "
            f"max_delete={params.max_delete}. Narrow the pattern or raise "
            "max_delete (up to 10000) explicitly. This guard catches "
            "accidentally-too-wide patterns like `*`."
        )

    deleted: list[str] = []
    failed: list[tuple[str, str]] = []  # (name, stderr)
    for name, _creation, _used in matches:
        try:
            d_rc, _d_out, d_err = await ssh.run_command(["zfs", "destroy", name])
        except ssh.SshError as exc:
            failed.append((name, str(exc)))
            continue
        if d_rc == 0:
            deleted.append(name)
        else:
            failed.append((name, d_err.strip() or f"rc={d_rc}"))

    lines = [
        f"## Bulk snapshot destroy on `{params.dataset}`",
        "",
        f"Pattern: `{params.pattern}`"
        f"{' (recursive)' if params.recursive else ''}  ",
        f"Matched: {len(matches)} | Deleted: {len(deleted)} | "
        f"Failed: {len(failed)}",
        "",
    ]
    if deleted:
        lines.append("### Deleted")
        for n in deleted:
            lines.append(f"- `{n}`")
        lines.append("")
    if failed:
        lines.append("### Failed")
        for n, e in failed:
            # First line of stderr is usually the human-readable reason.
            short_err = e.splitlines()[0] if e else "(no stderr)"
            lines.append(f"- `{n}` — {short_err}")
        lines.append("")
        lines.append(
            "_Common reasons for failure: snapshot has dependent clones, "
            "or the dataset is busy. Use `zfs holds <snap>` and "
            "`zfs list -t snapshot -r <ds>` to investigate._"
        )

    return "\n".join(lines).rstrip()
