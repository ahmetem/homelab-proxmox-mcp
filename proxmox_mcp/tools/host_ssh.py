"""Proxmox host SSH tools (full shell exec).

The user has explicitly opted into unrestricted command execution on the
Proxmox host itself. Safety reduces to:

- confirm=true on every exec
- destructive-pattern detection -> requires i_understand_data_loss=true
- every call written to _host_ssh_audit.log

Use sparingly: most operations on the Proxmox host should go through the
typed API tools (proxmox_list_disks, proxmox_zfs_*, etc.). This is an
escape hatch for the cases those don't cover (e.g. mounting USB, running
rsync, ad-hoc diagnostics).
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from proxmox_mcp import host_ssh, config
from proxmox_mcp.format import missing_confirm
from proxmox_mcp.mcp_instance import mcp


class HostExecInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(
        ...,
        description=(
            "Shell command to execute on the Proxmox host. Runs under the "
            "user's login shell (typically /bin/bash for root). Pipes, "
            "redirects, heredocs all work. Output capped at 1 MB per stream."
        ),
        min_length=1, max_length=16384,
    )
    timeout: float = Field(
        default=60.0,
        description="Per-call timeout in seconds.",
        ge=1.0, le=900.0,
    )
    confirm: bool = Field(
        default=False,
        description="Required. Set after confirming the command with the user.",
    )
    i_understand_data_loss: bool = Field(
        default=False,
        description=(
            "Required only when the command matches a destructive pattern "
            "(rm -rf, mkfs, dd of=/dev/..., zpool destroy, qm destroy, etc.)."
        ),
    )
    reason: Optional[str] = Field(default=None, max_length=200)


@mcp.tool(
    name="proxmox_host_exec",
    annotations={
        "title": "Execute Shell Command on Proxmox Host",
        "readOnlyHint": False, "destructiveHint": True,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_host_exec(params: HostExecInput) -> str:
    """Run a shell command on the Proxmox host (root by default).

    This is a full shell exec: no command allow-list, pipes/redirects work.
    Requires confirm=true on every call. Commands that match a destructive
    pattern (rm -rf, mkfs, dd of=/dev/..., zpool destroy, qm destroy,
    shutdown, fork bombs, etc.) also require i_understand_data_loss=true.

    Every call is recorded in _host_ssh_audit.log next to the package.

    Prefer the typed API tools (proxmox_list_disks, proxmox_zfs_*,
    proxmox_create_snapshot, etc.) when one exists for the task — this
    tool is intentionally an escape hatch for ad-hoc work like mounting
    USB disks, running rsync, or diagnostics.
    """
    if not params.confirm:
        return missing_confirm("proxmox_host_exec")

    danger = host_ssh.is_destructive(params.command)
    if danger and not params.i_understand_data_loss:
        return (
            f"Refused: command matches destructive pattern {danger!r}. "
            "Re-run with i_understand_data_loss=true if this is intentional."
        )

    try:
        rc, stdout, stderr = await host_ssh.exec_command(
            params.command, timeout=params.timeout
        )
    except Exception as exc:
        host_ssh.audit_log(
            params.command, None,
            note=f"FAILED: {type(exc).__name__}: {exc}",
        )
        return host_ssh.format_host_ssh_error(exc)

    host_ssh.audit_log(
        params.command, rc,
        note=("DESTRUCTIVE" if danger else "") + (f" reason={params.reason}" if params.reason else ""),
        stdout_preview=stdout[:200],
        stderr_preview=stderr[:200],
    )

    parts = [f"host=`{config.PROXMOX_SSH_HOST}`  rc={rc}"]
    if stdout.strip():
        parts.append("**stdout:**\n```\n" + stdout.rstrip() + "\n```")
    if stderr.strip():
        parts.append("**stderr:**\n```\n" + stderr.rstrip() + "\n```")
    if not stdout.strip() and not stderr.strip():
        parts.append("_(no output)_")
    return "\n\n".join(parts)
