"""Storage pool listing (read-only)."""
from __future__ import annotations

import json

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import fmt_bytes
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import NodeInput, ResponseFormat


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
        active = "\U0001F7E2" if s.get("active") else "\U0001F534"
        lines.append(
            f"- {active} **{s.get('storage')}** ({s.get('type')}) — "
            f"{fmt_bytes(used)}/{fmt_bytes(total)} ({pct:.0f}%) — "
            f"content: {s.get('content', '?')}"
        )
    return "\n".join(lines)
