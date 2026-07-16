"""Read-only integrity check for the hash-chained SSH audit logs.

Every proxmox_host_exec / proxmox_vm_exec (and the SSH-backed maintenance
tools) appends a hash-chained line via proxmox_mcp.audit. This tool recomputes
those chains and reports whether they are intact.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from proxmox_mcp import audit, host_ssh, vm_ssh
from proxmox_mcp.format import compact_json
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import ResponseFormat


_LOGS = {
    "host": host_ssh._AUDIT_PATH,
    "vm": vm_ssh._AUDIT_PATH,
}


class AuditVerifyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    which: str = Field(
        default="all",
        description="Which audit log to verify: 'host', 'vm', or 'all'.",
        pattern="^(host|vm|all)$",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


@mcp.tool(
    name="proxmox_audit_verify",
    annotations={
        "title": "Verify SSH Audit Log Integrity (hash chain)",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": False,
    },
)
async def proxmox_audit_verify(params: AuditVerifyInput) -> str:
    """Verify the tamper-evident hash chain of the SSH audit logs.

    Every host/guest shell exec is appended as a hash-chained line (SHA-256, or
    HMAC-SHA256 when PROXMOX_AUDIT_HMAC_KEY is set). This recomputes each chain
    and reports whether it is intact — a mismatch means a line was altered,
    deleted or reordered (or, for a keyed chain, that the key changed).
    Read-only.
    """
    targets = list(_LOGS) if params.which == "all" else [params.which]
    results = {name: audit.verify_chain(_LOGS[name]) for name in targets}

    if params.response_format == ResponseFormat.JSON:
        return compact_json(results)

    lines = ["## SSH audit log integrity", ""]
    for name in targets:
        r = results[name]
        path = _LOGS[name]
        if not r["exists"]:
            lines.append(f"- **{name}** (`{path.name}`): _no log yet._")
            continue
        mode = "HMAC-SHA256 (keyed)" if r["hmac"] else "SHA-256 (unkeyed)"
        icon = "\U0001F7E2" if r["ok"] else "\U0001F534"
        detail = f"{r['chained']} chained"
        if r["legacy"]:
            detail += f", {r['legacy']} legacy"
        lines.append(
            f"- {icon} **{name}** (`{path.name}`): "
            f"{'INTACT' if r['ok'] else 'BROKEN'} — {detail}, {mode}."
        )
        if not r["ok"] and r["first_break"]:
            fb = r["first_break"]
            lines.append(f"  - first problem at line {fb['line']}: {fb['why']}")
    if any(r.get("legacy") for r in results.values()):
        lines.append("")
        lines.append(
            "_Legacy lines predate hash-chaining; they can't be verified but "
            "don't break the chain that follows._"
        )
    return "\n".join(lines)
