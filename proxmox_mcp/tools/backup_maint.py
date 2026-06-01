"""Phase 7 - backup maintenance (narrow, auto-healable).

proxmox_cleanup_vzdump_snapshots removes ONLY leftover '@vzdump' ZFS snapshots
- the transient scratch a vzdump run creates and normally destroys at the end.
When a run is interrupted the snapshot is orphaned and every later backup of
that guest fails with 'dataset already exists'. Removal is reversible-
equivalent (recreated next run, no unique data) -> classified S2 (auto-heal).
Hard safety: targets NOTHING but names ending in '@vzdump', and skips snapshots
younger than min_age_minutes so it can never race a running backup.
"""
from __future__ import annotations

import re
import time
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from proxmox_mcp import host_ssh
from proxmox_mcp.config import require_ssh
from proxmox_mcp.format import missing_confirm
from proxmox_mcp.mcp_instance import mcp

_VZDUMP_SUFFIX = "@vzdump"
_DATASET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")


class CleanupVzdumpInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vmid: Optional[int] = Field(default=None, ge=100,
        description="Scope to one guest's datasets (subvol-<vmid>-disk-*, vm-<vmid>-disk-*).")
    dataset: Optional[str] = Field(default=None, max_length=256,
        description="Explicit dataset (recursive). Ignored if vmid given.")
    min_age_minutes: int = Field(default=30, ge=0, le=10080,
        description="Only remove @vzdump snapshots older than this (avoids racing a running backup).")
    dry_run: bool = Field(default=True,
        description="True (default): report only. False: actually remove (needs confirm=true).")
    confirm: bool = Field(default=False)
    reason: Optional[str] = Field(default=None, max_length=200)

    @field_validator("dataset")
    @classmethod
    def _v_ds(cls, v):
        if v and not _DATASET_RE.fullmatch(v):
            raise ValueError("Invalid dataset path.")
        return v


@mcp.tool(
    name="proxmox_cleanup_vzdump_snapshots",
    annotations={
        "title": "Clean stale @vzdump snapshots",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_cleanup_vzdump_snapshots(params: CleanupVzdumpInput) -> str:
    """Remove leftover '@vzdump' ZFS snapshots that block a guest's backups.
    Safe self-heal: ONLY '@vzdump'-suffixed snapshots older than min_age; never
    named/migration/autosnap snapshots."""
    cfg = require_ssh()
    if cfg:
        return cfg

    list_cmd = "zfs list -t snapshot -H -p -o name,creation"
    if params.dataset:
        list_cmd += " -r " + params.dataset
    try:
        rc, out, err = await host_ssh.exec_command(list_cmd, timeout=30)
    except Exception as exc:
        return host_ssh.format_host_ssh_error(exc)
    if rc != 0:
        return "Error listing snapshots (rc=%s): %s" % (rc, (err or out)[:400])

    now = time.time()
    cutoff = params.min_age_minutes * 60
    vmid_pat = None
    if params.vmid is not None:
        vmid_pat = re.compile(r"/(subvol|vm)-%d-disk-\d+%s$" % (params.vmid, re.escape(_VZDUMP_SUFFIX)))

    candidates = []
    too_young = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name, creation = parts[0], parts[1]
        if not name.endswith(_VZDUMP_SUFFIX):
            continue
        if vmid_pat and not vmid_pat.search(name):
            continue
        try:
            age = now - int(creation)
        except ValueError:
            age = 0
        (too_young if age < cutoff else candidates).append(name)

    if not candidates:
        msg = "_No stale @vzdump snapshots to clean._"
        if too_young:
            msg += "\n(%d found but younger than %dm - left alone.)" % (len(too_young), params.min_age_minutes)
        return msg

    listing = "\n".join("  - `%s`" % n for n in candidates)
    if params.dry_run:
        return "## Would remove %d stale @vzdump snapshot(s)\n\n%s\n\nRe-run with dry_run=false, confirm=true to remove." % (len(candidates), listing)
    if not params.confirm:
        return missing_confirm("proxmox_cleanup_vzdump_snapshots")

    removed = []
    failed = []
    for name in candidates:
        if not name.endswith(_VZDUMP_SUFFIX):
            continue
        try:
            drc, dout, derr = await host_ssh.exec_command("zfs destroy " + name, timeout=30)
        except Exception as exc:
            failed.append("%s: %s" % (name, host_ssh.format_host_ssh_error(exc)))
            continue
        host_ssh.audit_log("zfs destroy " + name, drc, note="cleanup_vzdump_snapshots", stderr_preview=derr)
        if drc == 0:
            removed.append(name)
        else:
            failed.append("%s: rc=%s %s" % (name, drc, (derr or dout)[:120]))

    lines = ["## Removed %d/%d stale @vzdump snapshot(s)" % (len(removed), len(candidates))]
    lines += ["  - removed `%s`" % n for n in removed]
    lines += ["  - FAILED %s" % f for f in failed]
    return "\n".join(lines)
