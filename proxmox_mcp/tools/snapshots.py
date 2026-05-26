"""Snapshot list / create / rollback / delete tools."""
from __future__ import annotations

import datetime as _dt
import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import missing_confirm, missing_data_loss_ack
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import (
    ResponseFormat,
    SnapshotCreateInput,
    SnapshotRollbackInput,
    VMInput,
)


class SnapshotDeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., min_length=1)
    vmid: int = Field(..., ge=100)
    vm_type: str = Field(default="qemu", pattern="^(qemu|lxc)$")
    snapname: str = Field(
        ...,
        description="Snapshot name to delete.",
        min_length=1, max_length=40,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$",
    )
    force: bool = Field(
        default=False,
        description=(
            "If true, force deletion even if the snapshot can't be cleanly "
            "removed (e.g. dangling references). Use with caution."
        ),
    )
    confirm: bool = Field(default=False)
    i_understand_data_loss: bool = Field(default=False)
    reason: Optional[str] = Field(default=None, max_length=200)


@mcp.tool(
    name="proxmox_list_snapshots",
    annotations={
        "title": "List VM Snapshots",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_list_snapshots(params: VMInput) -> str:
    """List snapshots for a specific VM or LXC container.

    Returns:
        str: For each snapshot: name, parent, description.
    """
    cfg = require_config()
    if cfg:
        return cfg
    try:
        snaps = await http_client.get(
            f"/nodes/{params.node}/{params.vm_type}/{params.vmid}/snapshot"
        )
    except Exception as exc:
        return http_client.format_http_error(exc)

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(snaps, indent=2, default=str)

    if not snaps:
        return f"_No snapshots for VM {params.vmid}._"

    lines = [f"## Snapshots for VM {params.vmid}", ""]
    for s in snaps:
        name = s.get("name", "?")
        if name == "current":
            lines.append(f"- \ud83d\udccd **current** — _you are here_")
            continue
        snaptime = s.get("snaptime", 0)
        try:
            ts = _dt.datetime.fromtimestamp(int(snaptime)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            ts = "?"
        desc = s.get("description", "").strip()
        lines.append(f"- \ud83d\udcf8 **{name}** — {ts}" + (f"  \n  {desc}" if desc else ""))
    return "\n".join(lines)


@mcp.tool(
    name="proxmox_create_snapshot",
    annotations={
        "title": "Create VM Snapshot",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def proxmox_create_snapshot(params: SnapshotCreateInput) -> str:
    """Create a snapshot of a VM or container. Requires confirm=true.

    Snapshot name must start with a letter; alphanumeric, dash, underscore only.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_create_snapshot")
    payload = {"snapname": params.snapname}
    if params.description:
        payload["description"] = params.description
    try:
        task_id = await http_client.post(
            f"/nodes/{params.node}/{params.vm_type}/{params.vmid}/snapshot",
            data=payload,
        )
    except Exception as exc:
        return http_client.format_http_error(exc)
    return (
        f"OK: Snapshot '{params.snapname}' creation started for "
        f"{params.vm_type} {params.vmid}. Task: {task_id}"
    )


@mcp.tool(
    name="proxmox_rollback_snapshot",
    annotations={
        "title": "Rollback to Snapshot",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def proxmox_rollback_snapshot(params: SnapshotRollbackInput) -> str:
    """Rollback VM/container to a snapshot. Data after snapshot is lost. Requires confirm=true."""
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_rollback_snapshot")
    try:
        task_id = await http_client.post(
            f"/nodes/{params.node}/{params.vm_type}/{params.vmid}"
            f"/snapshot/{params.snapname}/rollback"
        )
    except Exception as exc:
        return http_client.format_http_error(exc)
    return (
        f"OK: Rollback to '{params.snapname}' started for "
        f"{params.vm_type} {params.vmid}. Task: {task_id}"
    )


@mcp.tool(
    name="proxmox_delete_snapshot",
    annotations={
        "title": "Delete VM Snapshot",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_delete_snapshot(params: SnapshotDeleteInput) -> str:
    """Delete a VM/CT snapshot.

    The snapshot and any unique data it holds are removed permanently. The
    VM's current state is not affected. If the snapshot still references an
    unused source disk (e.g. after a move_disk with delete_source=false),
    that disk may also become eligible for cleanup once all snapshots are
    gone.

    Requires confirm=true AND i_understand_data_loss=true.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_delete_snapshot")
    if not params.i_understand_data_loss:
        return missing_data_loss_ack("proxmox_delete_snapshot")

    query = {"force": 1} if params.force else None
    try:
        task_id = await http_client.delete(
            f"/nodes/{params.node}/{params.vm_type}/{params.vmid}"
            f"/snapshot/{params.snapname}",
            params=query,
        )
    except Exception as exc:
        return http_client.format_http_error(exc)
    return (
        f"OK: Snapshot '{params.snapname}' deletion started for "
        f"{params.vm_type} {params.vmid}. Task: {task_id}"
    )
