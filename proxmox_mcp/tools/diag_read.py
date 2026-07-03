"""Phase 7 - read-only diagnostic tools.

Closes the agent's investigation gap: vzdump task logs, the scheduled-backup
config, and raw ZFS snapshots (e.g. a leftover '@vzdump') are only reachable
via host shell / non-inventory APIs. proxmox_host_exec covers them but is S4
(forbidden), so the autonomous consult could see backup SYMPTOMS via list/get
tools yet never the ROOT CAUSE. These four are strictly read-only and named to
auto-classify S1 - forensic reach without opening any mutating host shell.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from proxmox_mcp import host_ssh, http_client
from proxmox_mcp.config import require_config, require_ssh
from proxmox_mcp.format import compact_json, fmt_bytes
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import FormatInput, ResponseFormat

_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False,
              "idempotentHint": True, "openWorldHint": True}

_DATASET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")
_UPID_RE = re.compile(r"^UPID:[A-Za-z0-9._:-]+$")
_TYPEFILTER_RE = re.compile(r"^[a-z_]+$")


class ZfsListSnapshotsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset: Optional[str] = Field(default=None, max_length=256,
        description="Limit to one dataset + children, e.g. 'nvmepool/subvol-201-disk-0'. Omit for all.")
    contains: Optional[str] = Field(default=None, max_length=64,
        description="Only snapshots whose name contains this, e.g. 'vzdump'.")
    limit: int = Field(default=300, ge=1, le=5000)

    @field_validator("dataset")
    @classmethod
    def _v_ds(cls, v):
        if v and not _DATASET_RE.fullmatch(v):
            raise ValueError("Invalid dataset path.")
        return v

    @field_validator("contains")
    @classmethod
    def _v_c(cls, v):
        if v and not re.fullmatch(r"[A-Za-z0-9_.:@-]+", v):
            raise ValueError("Invalid 'contains' filter.")
        return v


@mcp.tool(name="proxmox_zfs_list_snapshots",
          annotations={"title": "List ZFS Snapshots (read-only)", **_READ_ONLY})
async def proxmox_zfs_list_snapshots(params: ZfsListSnapshotsInput) -> str:
    """List ZFS snapshots (name, used, referenced, creation), incl. transient
    '@vzdump' ones the PVE config-snapshot API never shows."""
    cfg = require_ssh()
    if cfg:
        return cfg
    cmd = "zfs list -t snapshot -H -p -o name,used,referenced,creation -s creation"
    if params.dataset:
        cmd += " -r " + params.dataset
    try:
        rc, out, err = await host_ssh.exec_command(cmd, timeout=30)
    except Exception as exc:
        return host_ssh.format_host_ssh_error(exc)
    if rc != 0:
        return "Error (rc=%s): %s" % (rc, (err or out)[:500])
    rows = []
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) >= 4 and (not params.contains or params.contains in p[0]):
            rows.append(p)
    rows = rows[: params.limit]
    if not rows:
        return "_No matching snapshots._"
    lines = ["## ZFS snapshots", ""]
    for name, used, refer, creation in ((r[0], r[1], r[2], r[3]) for r in rows):
        try:
            ts = datetime.fromtimestamp(int(creation), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        except Exception:
            ts = creation
        lines.append("- `%s` - used=%s ref=%s created=%sZ" % (name, fmt_bytes(used), fmt_bytes(refer), ts))
    return "\n".join(lines)


class ListTasksInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(default="pve")
    typefilter: Optional[str] = Field(default=None, description="e.g. 'vzdump'.")
    vmid: Optional[int] = Field(default=None, ge=100)
    errors_only: bool = Field(default=False)
    limit: int = Field(default=50, ge=1, le=500)

    @field_validator("typefilter")
    @classmethod
    def _v_tf(cls, v):
        if v and not _TYPEFILTER_RE.fullmatch(v):
            raise ValueError("typefilter: lowercase letters/underscores only.")
        return v


@mcp.tool(name="proxmox_list_tasks",
          annotations={"title": "List PVE Tasks (read-only)", **_READ_ONLY})
async def proxmox_list_tasks(params: ListTasksInput) -> str:
    """List recent PVE tasks (vzdump/snapshot/...) with start time, status, UPID."""
    cfg = require_config()
    if cfg:
        return cfg
    q = ["limit=%d" % params.limit]
    if params.typefilter:
        q.append("typefilter=%s" % params.typefilter)
    if params.vmid:
        q.append("vmid=%d" % params.vmid)
    if params.errors_only:
        q.append("errors=1")
    try:
        tasks = await http_client.get("/nodes/%s/tasks?%s" % (params.node, "&".join(q)))
    except Exception as exc:
        return http_client.format_http_error(exc)
    if not tasks:
        return "_No matching tasks._"
    lines = ["## PVE tasks", ""]
    for t in tasks:
        try:
            when = datetime.fromtimestamp(int(t.get("starttime")), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        except Exception:
            when = str(t.get("starttime"))
        status = t.get("status") or ("running" if t.get("endtime") is None else "?")
        lines.append("- `%sZ` **%s** id=%s status=%s upid=`%s`" % (
            when, t.get("type"), t.get("id", ""), status, t.get("upid")))
    return "\n".join(lines)


class TaskLogInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upid: str = Field(..., max_length=256, description="Full UPID from proxmox_list_tasks.")
    node: str = Field(default="pve")
    limit: int = Field(default=200, ge=1, le=5000)

    @field_validator("upid")
    @classmethod
    def _v_upid(cls, v):
        if not _UPID_RE.fullmatch(v):
            raise ValueError("Invalid UPID.")
        return v


@mcp.tool(name="proxmox_get_task_log",
          annotations={"title": "Get PVE Task Log (read-only)", **_READ_ONLY})
async def proxmox_get_task_log(params: TaskLogInput) -> str:
    """Read a PVE task's log lines (e.g. exactly why a vzdump failed)."""
    cfg = require_config()
    if cfg:
        return cfg
    try:
        rows = await http_client.get("/nodes/%s/tasks/%s/log?limit=%d" % (params.node, params.upid, params.limit))
    except Exception as exc:
        return http_client.format_http_error(exc)
    if not rows:
        return "_No log lines._"
    text = "\n".join(str(r.get("t", "")) for r in rows)
    return "## Task log `%s`\n\n```\n%s\n```" % (params.upid, text[:6000])


@mcp.tool(name="proxmox_list_backup_jobs",
          annotations={"title": "List Backup Jobs (read-only)", **_READ_ONLY})
async def proxmox_list_backup_jobs(params: FormatInput = FormatInput()) -> str:
    """List configured vzdump jobs (schedule, storage, vmids, retention)."""
    cfg = require_config()
    if cfg:
        return cfg
    try:
        jobs = await http_client.get("/cluster/backup")
    except Exception as exc:
        return http_client.format_http_error(exc)
    if params.response_format == ResponseFormat.JSON:
        return compact_json(jobs, fields=params.fields)
    if not jobs:
        return "_No backup jobs configured._"
    lines = ["## Backup jobs", ""]
    for j in jobs:
        lines.append("- **%s** schedule=`%s` storage=%s vmid=%s mode=%s enabled=%s" % (
            j.get("id", ""), j.get("schedule", "?"), j.get("storage", "?"),
            j.get("vmid", j.get("all", "?")), j.get("mode", "?"), j.get("enabled", "?")))
    return "\n".join(lines)
