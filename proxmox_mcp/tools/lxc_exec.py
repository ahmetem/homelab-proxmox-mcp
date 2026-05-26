"""LXC container exec via `pct exec` (host SSH wrapper).

This is a typed, narrowly-scoped sibling of proxmox_host_exec. Instead of
giving the agent unrestricted shell access on the Proxmox host, it lets
the agent run a command *inside* a specific LXC container.

Mechanically: ssh to the Proxmox host, then run
    pct exec <vmid> -- /bin/sh -c '<command>'

Compared to proxmox_host_exec + ad-hoc `pct exec`:
- vmid is a typed parameter (no shell injection on the container ID)
- destructive-pattern detection runs on the user's *inner* command, not
  the `pct exec` envelope - so `rm -rf /something` inside the CT is
  correctly flagged
- the audit log records the vmid distinctly

`pct exec` runs the command as root inside the CT namespace from the
Proxmox host; it does NOT require SSH inside the container, so works
even when the CT has no sshd.

For VM (qemu) guests use `proxmox_vm_exec` instead - it goes over real
SSH using vm_ssh_hosts.json aliases.
"""
from __future__ import annotations

import shlex
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from proxmox_mcp import config, host_ssh
from proxmox_mcp.format import missing_confirm
from proxmox_mcp.mcp_instance import mcp


class LxcExecInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vmid: int = Field(
        ...,
        description="LXC container ID to exec into (e.g. 200, 201, 202).",
        ge=100,
        le=999999999,
    )
    command: str = Field(
        ...,
        description=(
            "Shell command to execute inside the container. Runs under "
            "/bin/sh -c via `pct exec`. Pipes, redirects, heredocs all work. "
            "Output capped at 1 MB per stream."
        ),
        min_length=1,
        max_length=16384,
    )
    timeout: float = Field(
        default=60.0,
        description="Per-call timeout in seconds.",
        ge=1.0,
        le=900.0,
    )
    confirm: bool = Field(
        default=False,
        description="Required. Set after confirming the command with the user.",
    )
    i_understand_data_loss: bool = Field(
        default=False,
        description=(
            "Required only when the command matches a destructive pattern "
            "(rm -rf, mkfs, dd of=/dev/..., shutdown, etc.) - checked "
            "against the inner command, NOT the `pct exec` wrapper."
        ),
    )
    reason: Optional[str] = Field(default=None, max_length=200)


@mcp.tool(
    name="proxmox_lxc_exec",
    annotations={
        "title": "Execute Shell Command in LXC Container",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def proxmox_lxc_exec(params: LxcExecInput) -> str:
    """Run a shell command inside a Proxmox LXC container via `pct exec`.

    Typical uses on this homelab:
      - vmid=200 -> postgres CT: psql queries, pg_dump, vacuum status
      - vmid=201 -> immich CT: docker compose ps/logs
      - vmid=202 -> n8n CT: systemctl status n8n, journalctl -u n8n
      - vmid=203 -> qbittorrent CT: rtorrent / qbt-nox state
      - vmid=205 -> PBS CT: proxmox-backup-manager datastore list

    `pct exec` runs the command as root inside the CT namespace from the
    Proxmox host. The CT does not need to be reachable on the network,
    and the host's SSH key is the only credential - no extra setup per
    container.

    Requires confirm=true on every call. Commands matching a destructive
    pattern also require i_understand_data_loss=true.
    """
    if not params.confirm:
        return missing_confirm("proxmox_lxc_exec")

    # Destructive-pattern detection runs against the INNER command, not the
    # `pct exec` envelope. This is the right granularity: `pct exec 200 --
    # /bin/sh -c 'rm -rf /'` should be flagged for the `rm -rf` part.
    danger = host_ssh.is_destructive(params.command)
    if danger and not params.i_understand_data_loss:
        return (
            f"Refused: command matches destructive pattern {danger!r}. "
            "Re-run with i_understand_data_loss=true if this is intentional."
        )

    # Build wrapper: `pct exec <vmid> -- /bin/sh -c '<command>'`.
    # shlex.quote handles single-quote escaping inside the user's command.
    quoted_cmd = shlex.quote(params.command)
    wrapper = f"pct exec {params.vmid} -- /bin/sh -c {quoted_cmd}"

    try:
        rc, stdout, stderr = await host_ssh.exec_command(
            wrapper, timeout=params.timeout
        )
    except Exception as exc:
        host_ssh.audit_log(
            wrapper, None,
            note=f"FAILED lxc_exec ct={params.vmid}: {type(exc).__name__}: {exc}",
        )
        return host_ssh.format_host_ssh_error(exc)

    host_ssh.audit_log(
        wrapper, rc,
        note=(f"lxc_exec ct={params.vmid}"
              + (" DESTRUCTIVE" if danger else "")
              + (f" reason={params.reason}" if params.reason else "")),
        stdout_preview=stdout[:200],
        stderr_preview=stderr[:200],
    )

    parts = [f"CT {params.vmid} on `{config.PROXMOX_SSH_HOST}`  rc={rc}"]
    if stdout.strip():
        parts.append("**stdout:**\n```\n" + stdout.rstrip() + "\n```")
    if stderr.strip():
        parts.append("**stderr:**\n```\n" + stderr.rstrip() + "\n```")
    if not stdout.strip() and not stderr.strip():
        parts.append("_(no output)_")
    return "\n\n".join(parts)
