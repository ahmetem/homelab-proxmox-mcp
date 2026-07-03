"""Phase 2: cluster-level storage management.

Backed by Proxmox REST endpoints:
  - GET    /storage              -> list all storage entries
  - POST   /storage              -> create
  - DELETE /storage/{storage}    -> remove (does NOT delete data)

These manage the *cluster-wide* /etc/pve/storage.cfg entries, not the
per-node disk pools themselves. proxmox_lvm_manage / proxmox_zfs_pool_manage
already accept add_storage=true to do this in one shot; this tool is for
attaching pre-existing pools or for cleanup.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import compact_json, missing_confirm
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import FormatInput, ResponseFormat


_STORAGE_ID = r"^[A-Za-z][A-Za-z0-9_.-]*$"


class StorageConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(
        ...,
        description=(
            "'add_zfs' registers an existing ZFS pool, 'add_dir' registers a "
            "filesystem directory, 'remove' deletes the config entry (data is "
            "NOT touched). All require confirm=true."
        ),
        pattern="^(add_zfs|add_dir|remove)$",
    )
    storage: str = Field(
        ..., description="Storage ID in PVE.",
        min_length=1, max_length=64, pattern=_STORAGE_ID,
    )
    pool: Optional[str] = Field(
        default=None,
        description="add_zfs only: existing ZFS pool (or pool/dataset) to expose.",
        max_length=128,
    )
    path: Optional[str] = Field(
        default=None,
        description="add_dir only: absolute path on the node.",
        max_length=256, pattern=r"^/[A-Za-z0-9/_.+-]+$",
    )
    content: Optional[str] = Field(
        default=None,
        description=(
            "Comma-separated content types. Defaults: add_zfs → "
            "'rootdir,images', add_dir → 'iso,vztmpl,backup'."
        ),
        max_length=128,
    )
    sparse: bool = Field(
        default=True, description="add_zfs only: sparse zvols (thin provisioning)."
    )
    nodes: Optional[str] = Field(
        default=None,
        description="Optional comma-separated node restriction (default: all nodes).",
        max_length=256,
    )
    confirm: bool = Field(default=False)
    reason: Optional[str] = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _check_required(self):
        if self.action == "add_zfs" and not self.pool:
            raise ValueError("action='add_zfs' requires 'pool'.")
        if self.action == "add_dir" and not self.path:
            raise ValueError("action='add_dir' requires 'path'.")
        return self


@mcp.tool(
    name="proxmox_list_cluster_storage",
    annotations={
        "title": "List Cluster Storage Configuration",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def proxmox_list_cluster_storage(params: FormatInput = FormatInput()) -> str:
    """List all storage entries defined in the cluster configuration
    (/etc/pve/storage.cfg — what is *defined*; use proxmox_list_storage for
    what is *active* on a node)."""
    cfg = require_config()
    if cfg:
        return cfg
    try:
        items = await http_client.get("/storage")
    except Exception as exc:
        return http_client.format_http_error(exc)

    if params.response_format == ResponseFormat.JSON:
        return compact_json(items, fields=params.fields)

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
    name="proxmox_storage_config",
    annotations={
        "title": "Add / Remove Cluster Storage Entry",
        "readOnlyHint": False, "destructiveHint": True,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_storage_config(params: StorageConfigInput) -> str:
    """Add or remove a cluster storage configuration entry.

    add_zfs: expose an existing ZFS pool (not needed if the pool was created
    with add_storage=true). add_dir: expose a directory for ISO/template/
    backup content. remove: delete only the PVE storage record — the
    underlying pool/directory and its data are NOT touched.

    Requires confirm=true.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm(f"proxmox_storage_config (action={params.action})")

    try:
        if params.action == "remove":
            await http_client.delete(f"/storage/{params.storage}")
            return (
                f"OK: Storage entry '{params.storage}' removed. "
                "Underlying data was NOT touched."
            )
        if params.action == "add_zfs":
            payload = {
                "storage": params.storage, "type": "zfspool", "pool": params.pool,
                "content": params.content or "rootdir,images",
                "sparse": 1 if params.sparse else 0,
            }
            desc = f"type=zfspool, pool={params.pool}"
        else:  # add_dir
            payload = {
                "storage": params.storage, "type": "dir", "path": params.path,
                "content": params.content or "iso,vztmpl,backup",
            }
            desc = f"type=dir, path={params.path}"
        if params.nodes:
            payload["nodes"] = params.nodes
        result = await http_client.post("/storage", data=payload)
    except Exception as exc:
        return http_client.format_http_error(exc)
    return f"OK: Storage '{params.storage}' ({desc}) registered. Response: {result}"
