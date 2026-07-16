"""Backup list / create / restore tools."""
from __future__ import annotations

import datetime as _dt

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import (
    compact_json,
    dry_run_preview,
    fmt_bytes,
    missing_confirm,
    missing_data_loss_ack,
)
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import (
    BackupCreateInput,
    BackupListInput,
    BackupRestoreInput,
    ResponseFormat,
)


@mcp.tool(
    name="proxmox_list_backups",
    annotations={
        "title": "List Backups",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_list_backups(params: BackupListInput) -> str:
    """List backup files on a storage, newest first.

    Optional filters: vmid (one guest only), limit (newest N, default 50).
    """
    cfg = require_config()
    if cfg:
        return cfg
    try:
        backups = await http_client.get(
            f"/nodes/{params.node}/storage/{params.storage}/content",
            params={"content": "backup"},
        )
    except Exception as exc:
        return http_client.format_http_error(exc)

    backups = [
        b for b in (backups or [])
        if params.vmid is None or b.get("vmid") == params.vmid
    ]
    backups = sorted(backups, key=lambda x: x.get("ctime", 0), reverse=True)
    total = len(backups)
    backups = backups[: params.limit]

    if params.response_format == ResponseFormat.JSON:
        return compact_json(backups, fields=params.fields)

    if not backups:
        scope = f" for VM {params.vmid}" if params.vmid else ""
        return f"_No backups on `{params.storage}`{scope}._"

    lines = [f"## Backups on `{params.storage}` (node `{params.node}`)", ""]
    if total > len(backups):
        lines.append(f"_Showing newest {len(backups)} of {total}._")
        lines.append("")
    for b in backups:
        volid = b.get("volid", "?")
        size = fmt_bytes(b.get("size", 0))
        vmid = b.get("vmid", "?")
        ctime = b.get("ctime", 0)
        try:
            ts = _dt.datetime.fromtimestamp(int(ctime)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            ts = "?"
        lines.append(f"- **VM {vmid}** — {size} — {ts}  \n  `{volid}`")
    return "\n".join(lines)


@mcp.tool(
    name="proxmox_create_backup",
    annotations={
        "title": "Create VM Backup",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def proxmox_create_backup(params: BackupCreateInput) -> str:
    """Create a backup (vzdump) of a VM/container. Requires confirm=true.

    Modes: snapshot (minimal downtime, recommended), suspend, stop.
    Set wait_seconds>0 to poll the backup task and report the result inline.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_create_backup")
    payload = {
        "vmid": params.vmid,
        "storage": params.storage,
        "mode": params.mode,
        "compress": params.compress,
    }
    try:
        task_id = await http_client.post(f"/nodes/{params.node}/vzdump", data=payload)
    except Exception as exc:
        return http_client.format_http_error(exc)
    suffix = await http_client.wait_for_task(params.node, task_id, params.wait_seconds)
    if not suffix:
        suffix = " Backup runs in background; use proxmox_list_backups to verify."
    return (
        f"OK: Backup of VM {params.vmid} started on storage "
        f"`{params.storage}` (mode={params.mode}, compress={params.compress}). "
        f"Task: {task_id}.{suffix}"
    )


@mcp.tool(
    name="proxmox_restore_backup",
    annotations={
        "title": "Restore VM/CT from Backup",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def proxmox_restore_backup(params: BackupRestoreInput) -> str:
    """Restore a VM or LXC container from a backup archive.

    Refuses to overwrite an existing VMID unless force=true AND
    i_understand_data_loss=true (overwrite destroys the current guest's disks
    first). Requires confirm=true. Set wait_seconds>0 to poll the restore task.
    Pass dry_run=true to preview the endpoint + payload without restoring.
    """
    cfg = require_config()
    if cfg:
        return cfg

    # Refuse silent overwrite: probe whether the target VMID already exists.
    # /cluster/resources is a single cheap call that lists every VM/CT.
    try:
        resources = await http_client.get(
            "/cluster/resources", params={"type": "vm"}
        )
    except Exception as exc:
        return http_client.format_http_error(exc)

    existing = next(
        (r for r in resources if r.get("vmid") == params.vmid),
        None,
    )
    if existing is not None and not params.force:
        return (
            f"Refused: VMID {params.vmid} already exists on node "
            f"`{existing.get('node', '?')}` "
            f"({existing.get('type', '?')} `{existing.get('name', '?')}`, "
            f"status={existing.get('status', '?')}). "
            "Re-run with force=true AND i_understand_data_loss=true to "
            "overwrite, or pick a different vmid."
        )

    # The qemu and lxc endpoints differ in parameter names — qemu uses
    # `archive`, lxc uses `ostemplate` + `restore=1`.
    if params.vm_type == "qemu":
        payload: dict = {
            "vmid": params.vmid,
            "archive": params.archive,
        }
        endpoint = f"/nodes/{params.node}/qemu"
    else:  # lxc
        payload = {
            "vmid": params.vmid,
            "ostemplate": params.archive,
            "restore": 1,
        }
        endpoint = f"/nodes/{params.node}/lxc"
    if params.force:
        payload["force"] = 1
    if params.storage:
        payload["storage"] = params.storage
    if params.start_after_restore:
        payload["start"] = 1

    if params.dry_run:
        effect = ("OVERWRITE existing guest (force)" if existing is not None
                  else "fresh restore")
        return dry_run_preview("POST", endpoint, payload) + f"\nEffect: {effect}."
    if not params.confirm:
        return missing_confirm("proxmox_restore_backup")
    if params.force and not params.i_understand_data_loss:
        return missing_data_loss_ack("proxmox_restore_backup")

    try:
        task_id = await http_client.post(endpoint, data=payload)
    except Exception as exc:
        return http_client.format_http_error(exc)

    note = "overwriting existing guest" if existing is not None else "fresh restore"
    suffix = await http_client.wait_for_task(params.node, task_id, params.wait_seconds)
    if not suffix:
        suffix = (
            " Restore runs in background; large guests can take many minutes."
        )
    return (
        f"OK: {params.vm_type.upper()} restore of VMID {params.vmid} "
        f"started from `{params.archive}` ({note}). Task: {task_id}.{suffix}"
    )
