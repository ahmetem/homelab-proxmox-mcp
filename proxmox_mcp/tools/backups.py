"""Backup list / create tools."""
from __future__ import annotations

import datetime as _dt
import json

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import fmt_bytes, missing_confirm
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import BackupCreateInput, ResponseFormat, StorageInput


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
