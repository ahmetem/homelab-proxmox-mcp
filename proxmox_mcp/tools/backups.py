"""Backup list / create / restore tools."""
from __future__ import annotations

import datetime as _dt
import json

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import fmt_bytes, missing_confirm, missing_data_loss_ack
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import (
    BackupCreateInput,
    BackupRestoreInput,
    ResponseFormat,
    StorageInput,
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
async def proxmox_list_backups(params: StorageInput) -> str:
    """List backup files on a storage.

    Returns:
        str: For each backup: filename, VMID, size, creation time.
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

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(backups, indent=2, default=str)

    if not backups:
        return f"_No backups on `{params.storage}`._"

    lines = [f"## Backups on `{params.storage}` (node `{params.node}`)", ""]
    for b in sorted(backups, key=lambda x: x.get("ctime", 0), reverse=True):
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
    """Create a backup of a VM/container. Requires confirm=true.

    Modes:
      - snapshot: quick backup using snapshots (minimal downtime, recommended)
      - suspend: suspends VM during backup (consistency)
      - stop: stops VM during backup (max consistency, max downtime)

    Compression: none, lzo, gzip, zstd (zstd recommended)
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
    return (
        f"OK: Backup of VM {params.vmid} started on storage "
        f"`{params.storage}` (mode={params.mode}, compress={params.compress}). "
        f"Task: {task_id}. Backup runs in background; "
        "use proxmox_list_backups to verify completion."
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

    Two flavors depending on vm_type:
      - qemu: POST /nodes/{node}/qemu with `archive` parameter
      - lxc : POST /nodes/{node}/lxc  with `ostemplate=<archive>` + `restore=1`

    Refuses to overwrite an existing VMID unless force=true AND
    i_understand_data_loss=true. Overwrite destroys the current guest's
    disks before restore.

    Requires confirm=true. Restore runs in the background; the returned
    task ID lets you track progress via Proxmox web UI or
    `proxmox_get_vm_status` once complete.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_restore_backup")
    if params.force and not params.i_understand_data_loss:
        return missing_data_loss_ack("proxmox_restore_backup")

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

    # Build the payload. The qemu and lxc endpoints differ in parameter
    # names — qemu uses `archive`, lxc uses `ostemplate` + `restore=1`.
    if params.vm_type == "qemu":
        payload: dict = {
            "vmid": params.vmid,
            "archive": params.archive,
        }
        if params.force:
            payload["force"] = 1
        if params.storage:
            payload["storage"] = params.storage
        if params.start_after_restore:
            payload["start"] = 1
        endpoint = f"/nodes/{params.node}/qemu"
    else:  # lxc
        payload = {
            "vmid": params.vmid,
            "ostemplate": params.archive,
            "restore": 1,
        }
        if params.force:
            payload["force"] = 1
        if params.storage:
            payload["storage"] = params.storage
        if params.start_after_restore:
            payload["start"] = 1
        endpoint = f"/nodes/{params.node}/lxc"

    try:
        task_id = await http_client.post(endpoint, data=payload)
    except Exception as exc:
        return http_client.format_http_error(exc)

    note = "overwriting existing guest" if existing is not None else "fresh restore"
    return (
        f"OK: {params.vm_type.upper()} restore of VMID {params.vmid} "
        f"started from `{params.archive}` ({note}). "
        f"Task: {task_id}. Restore runs in background; for large guests "
        "this can take many minutes. Track via Proxmox web UI or "
        "proxmox_get_vm_status once complete."
    )
