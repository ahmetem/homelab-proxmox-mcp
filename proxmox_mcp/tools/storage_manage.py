"""Phase 2: cluster-level storage management.

Backed by Proxmox REST endpoints:
  - GET    /storage              -> list all storage entries
  - POST   /storage              -> create
  - DELETE /storage/{storage}    -> remove (does NOT delete data)
  - PUT    /storage/{storage}    -> update flags (enable/disable, content types)

These manage the *cluster-wide* /etc/pve/storage.cfg entries, not the
per-node disk pools themselves. proxmox_create_lvm_vg / proxmox_create_zfs_pool
already accept add_storage=true to do this in one shot; these tools are for
attaching pre-existing pools or for cleanup.
"""
from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import missing_confirm
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import FormatInput, ResponseFormat


_STORAGE_ID = r"^[A-Za-z][A-Za-z0-9_.-]*$"


class StorageAddZfsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    storage: str = Field(
        ..., description="Storage ID to expose in PVE (alphanumeric, dot, dash, underscore).",
        min_length=1, max_length=64, pattern=_STORAGE_ID,
    )
    pool: str = Field(
        ..., description="Existing ZFS pool (or pool/dataset path) to expose.",
        min_length=1, max_length=128,
    )
    content: str = Field(
        default="rootdir,images",
        description=(
            "Comma-separated content types this storage accepts. "
            "Typical for ZFS: 'rootdir,images'."
        ),
        max_length=128,
    )
    sparse: bool = Field(default=True, description="Use sparse zvols (thin provisioning).")
    nodes: Optional[str] = Field(
        default=None,
        description="Optional comma-separated node restriction (default: all nodes).",
        max_length=256,
    )
    confirm: bool = Field(default=False)


class StorageAddDirInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    storage: str = Field(..., min_length=1, max_length=64, pattern=_STORAGE_ID)
    path: str = Field(
        ..., description="Absolute path on the node hosting the storage.",
        min_length=1, max_length=256, pattern=r"^/[A-Za-z0-9/_.+-]+$",
    )
    content: str = Field(default="iso,vztmpl,backup", max_length=128)
    nodes: Optional[str] = Field(default=None, max_length=256)
    confirm: bool = Field(default=False)


class StorageRemoveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    storage: str = Field(..., min_length=1, max_length=64, pattern=_STORAGE_ID)
    confirm: bool = Field(default=False)
    reason: Optional[str] = Field(default=None, max_length=200)


@mcp.tool(
    name="proxmox_list_cluster_storage",
    annotations={
        "title": "List Cluster Storage Configuration",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def proxmox_list_cluster_storage(params: FormatInput) -> str:
    """List all storage entries defined in the cluster configuration.

    This is /etc/pve/storage.cfg — what is *defined*, not what is *active*
    on a specific node (use proxmox_list_storage for that).
    """
    cfg = require_config()
    if cfg:
        return cfg
    try:
        items = await http_client.get("/storage")
    except Exception as exc:
        return http_client.format_http_error(exc)

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(items, indent=2, default=str)

    if not items:
        return "_No cluster storage entries defined._"

    lines = [
        "## Cluster storage entries",
        "",
        "| ID | Type | Content | Backing | Nodes | Disabled |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for s in items:
        storage = s.get("storage", "?")
        stype = s.get("type", "?")
        content = s.get("content", "?")
        backing = s.get("pool") or s.get("vgname") or s.get("thinpool") or s.get("path") or ""
        nodes = s.get("nodes", "all")
        disabled = "yes" if s.get("disable") else "no"
        lines.append(
            f"| `{storage}` | {stype} | {content} | `{backing}` | {nodes} | {disabled} |"
        )
    return "\n".join(lines)


@mcp.tool(
    name="proxmox_add_zfs_storage",
    annotations={
        "title": "Register Existing ZFS Pool as PVE Storage",
        "readOnlyHint": False, "destructiveHint": False,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_add_zfs_storage(params: StorageAddZfsInput) -> str:
    """Register an existing ZFS pool as a Proxmox storage entry.

    Use this when a pool exists (created outside of PVE, or imported) but
    isn't visible in PVE yet. If you created the pool with
    proxmox_create_zfs_pool and add_storage=true, you do NOT need this.

    Requires confirm=true.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_add_zfs_storage")

    payload = {
        "storage": params.storage, "type": "zfspool", "pool": params.pool,
        "content": params.content, "sparse": 1 if params.sparse else 0,
    }
    if params.nodes:
        payload["nodes"] = params.nodes

    try:
        result = await http_client.post("/storage", data=payload)
    except Exception as exc:
        return http_client.format_http_error(exc)
    return f"OK: Storage '{params.storage}' (type=zfspool, pool={params.pool}) registered. Response: {result}"


@mcp.tool(
    name="proxmox_add_dir_storage",
    annotations={
        "title": "Register Directory as PVE Storage",
        "readOnlyHint": False, "destructiveHint": False,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_add_dir_storage(params: StorageAddDirInput) -> str:
    """Register a filesystem directory as a Proxmox storage entry.

    Useful for ISO / template / backup storage on top of mounted FS or
    a ZFS dataset. Requires confirm=true.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_add_dir_storage")

    payload = {"storage": params.storage, "type": "dir", "path": params.path, "content": params.content}
    if params.nodes:
        payload["nodes"] = params.nodes

    try:
        result = await http_client.post("/storage", data=payload)
    except Exception as exc:
        return http_client.format_http_error(exc)
    return f"OK: Directory storage '{params.storage}' at `{params.path}` registered. Response: {result}"


@mcp.tool(
    name="proxmox_remove_storage",
    annotations={
        "title": "Remove Storage Configuration Entry",
        "readOnlyHint": False, "destructiveHint": True,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def proxmox_remove_storage(params: StorageRemoveInput) -> str:
    """Remove a storage entry from the cluster configuration.

    This only deletes the PVE storage record — the underlying pool, VG,
    or directory and its data are NOT touched. Requires confirm=true.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm("proxmox_remove_storage")

    try:
        await http_client.delete(f"/storage/{params.storage}")
    except Exception as exc:
        return http_client.format_http_error(exc)
    return f"OK: Storage entry '{params.storage}' removed. Underlying data was NOT touched."
