"""Storage pool listing + per-storage content breakdown (read-only)."""
from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import fmt_bytes
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import NodeInput, ResponseFormat


class StorageUsageDetailInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., description="Node name (e.g. 'pve').", min_length=1)
    storage: str = Field(
        ...,
        description="Storage name as shown by proxmox_list_storage.",
        min_length=1,
        max_length=64,
    )
    content_filter: Optional[str] = Field(
        default=None,
        description=(
            "Optional comma-separated content types to include "
            "(e.g. 'images,backup'). Available: images, iso, backup, "
            "vztmpl, snippets, rootdir. If omitted, every content type "
            "on this storage is shown."
        ),
        max_length=128,
        pattern=r"^[a-zA-Z,]+$",
    )
    top_n: int = Field(
        default=10,
        description="How many largest items to list individually.",
        ge=1,
        le=100,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


@mcp.tool(
    name="proxmox_list_storage",
    annotations={
        "title": "List Storage Pools",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_list_storage(params: NodeInput) -> str:
    """List storage pools on a node with usage information.

    Returns:
        str: For each storage: name, type, usage, content types.
    """
    cfg = require_config()
    if cfg:
        return cfg
    try:
        storages = await http_client.get(f"/nodes/{params.node}/storage")
    except Exception as exc:
        return http_client.format_http_error(exc)

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(storages, indent=2, default=str)

    if not storages:
        return "_No storage pools found._"

    lines = [f"## Storage on `{params.node}`", ""]
    for s in storages:
        used = s.get("used", 0)
        total = s.get("total", 0)
        pct = (used / total * 100) if total else 0
        active = "🟢" if s.get("active") else "🔴"
        lines.append(
            f"- {active} **{s.get('storage')}** ({s.get('type')}) — "
            f"{fmt_bytes(used)}/{fmt_bytes(total)} ({pct:.0f}%) — "
            f"content: {s.get('content', '?')}"
        )
    return "\n".join(lines)


@mcp.tool(
    name="proxmox_storage_usage_detail",
    annotations={
        "title": "Storage Content Breakdown by Type",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_storage_usage_detail(params: StorageUsageDetailInput) -> str:
    """Detailed content breakdown of a single storage: per-type totals
    plus a top-N list of largest items.

    Use for capacity planning ("which VM is eating my backup storage?"),
    investigating sudden growth, or deciding what to prune. Pairs well
    with `proxmox_list_storage` (which gives the aggregate usage).

    Each item from /nodes/{node}/storage/{storage}/content is grouped by
    its `content` field. Item sizes come straight from the API — for
    PBS-backed storages this is logical (pre-dedup) size, not on-disk
    chunk-store size.

    Returns:
        str: Markdown report with two sections — summary table by
             content type, and top-N items by size.
    """
    cfg = require_config()
    if cfg:
        return cfg

    query = {"content": params.content_filter} if params.content_filter else None
    try:
        items = await http_client.get(
            f"/nodes/{params.node}/storage/{params.storage}/content",
            params=query,
        )
    except Exception as exc:
        return http_client.format_http_error(exc)

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(items, indent=2, default=str)

    if not items:
        scope = (
            f" (filter: `{params.content_filter}`)"
            if params.content_filter else ""
        )
        return f"_No items on `{params.storage}`{scope}._"

    # Group by content type
    by_type: dict[str, list[dict]] = {}
    for it in items:
        ct = it.get("content", "?")
        by_type.setdefault(ct, []).append(it)

    lines = [
        f"## Storage `{params.storage}` on `{params.node}` — content detail",
        "",
        "### Summary by content type",
        "",
        "| Type | Items | Total size |",
        "| --- | --- | --- |",
    ]
    grand_total = 0
    # Sort by total size descending
    by_type_sorted = sorted(
        by_type.items(),
        key=lambda kv: -sum((g.get("size") or 0) for g in kv[1]),
    )
    for ct, group in by_type_sorted:
        total = sum((g.get("size") or 0) for g in group)
        grand_total += total
        lines.append(f"| {ct} | {len(group)} | {fmt_bytes(total)} |")
    lines.append(
        f"| **Total** | **{len(items)}** | **{fmt_bytes(grand_total)}** |"
    )
    lines.append("")

    # Top N items by size
    top = sorted(items, key=lambda x: -(x.get("size") or 0))[: params.top_n]
    lines.append(f"### Top {min(params.top_n, len(top))} items by size")
    lines.append("")
    lines.append("| Volid | Type | Size | VMID |")
    lines.append("| --- | --- | --- | --- |")
    for it in top:
        volid = it.get("volid", "?")
        ct = it.get("content", "?")
        sz = fmt_bytes(it.get("size") or 0)
        vmid = it.get("vmid", "-")
        lines.append(f"| `{volid}` | {ct} | {sz} | {vmid} |")

    return "\n".join(lines).rstrip()
