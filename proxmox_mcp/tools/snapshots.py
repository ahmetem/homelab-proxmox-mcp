"""Snapshot list + manage (create/rollback/delete) tools."""
from __future__ import annotations

import datetime as _dt

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import compact_json, missing_confirm, missing_data_loss_ack
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import ResponseFormat, SnapshotManageInput, VMInput
from proxmox_mcp.operator_ack import ack_refusal, gated


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
    """List snapshots for a specific VM or LXC container."""
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
        return compact_json(snaps, fields=params.fields)

    if not snaps:
        return f"_No snapshots for VM {params.vmid}._"

    lines = [f"## Snapshots for VM {params.vmid}", ""]
    for s in snaps:
        name = s.get("name", "?")
        if name == "current":
            lines.append(f"- \U0001F4CD **current** — _you are here_")
            continue
        snaptime = s.get("snaptime", 0)
        try:
            ts = _dt.datetime.fromtimestamp(int(snaptime)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            ts = "?"
        desc = s.get("description", "").strip()
        lines.append(f"- \U0001F4F8 **{name}** — {ts}" + (f"  \n  {desc}" if desc else ""))
    return "\n".join(lines)


@mcp.tool(
    name="proxmox_snapshot",
    annotations={
        "title": "Create / Rollback / Delete VM Snapshot",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
@gated(
    "proxmox_snapshot",
    "rollback discards state newer than the snapshot; delete is permanent",
)
async def proxmox_snapshot(params: SnapshotManageInput, operator_ack=None) -> str:
    """Manage a VM/CT snapshot: action='create', 'rollback', or 'delete'.

    Gates: all actions require confirm=true, and where the client supports it the
    operator is asked to approve directly. 'rollback' discards state newer
    than the snapshot. 'delete' also requires i_understand_data_loss=true —
    the snapshot and its unique data are removed permanently (current state
    is unaffected). Set wait_seconds>0 to get the task result inline.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm(f"proxmox_snapshot (action={params.action})")
    # Every model-set flag is checked before the operator is asked, so a human is
    # never prompted for a call that a later flag gate would refuse anyway.
    if params.action == "delete" and not params.i_understand_data_loss:
        return missing_data_loss_ack("proxmox_snapshot (action=delete)")
    refused = ack_refusal(operator_ack, f"proxmox_snapshot (action={params.action})")
    if refused:
        return refused

    base = f"/nodes/{params.node}/{params.vm_type}/{params.vmid}/snapshot"
    try:
        if params.action == "create":
            payload = {"snapname": params.snapname}
            if params.description:
                payload["description"] = params.description
            task_id = await http_client.post(base, data=payload)
            verb = "creation"
        elif params.action == "rollback":
            task_id = await http_client.post(f"{base}/{params.snapname}/rollback")
            verb = "rollback"
        else:  # delete (data-loss ack already checked above)
            query = {"force": 1} if params.force else None
            task_id = await http_client.delete(
                f"{base}/{params.snapname}", params=query
            )
            verb = "deletion"
    except Exception as exc:
        return http_client.format_http_error(exc)

    suffix = await http_client.wait_for_task(params.node, task_id, params.wait_seconds)
    return (
        f"OK: Snapshot '{params.snapname}' {verb} started for "
        f"{params.vm_type} {params.vmid}. Task: {task_id}.{suffix}"
    )
