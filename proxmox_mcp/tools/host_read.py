"""Read-only host shell exec (allow-listed).

`proxmox_host_exec` runs arbitrary commands and can destroy things, so it is
deliberately kept out of the read-only auditor subagent. This module adds a
*safe* alternative: `proxmox_host_read_exec` accepts only allow-listed,
non-mutating commands so it can be granted to a read-only agent for host
diagnostics (cat a config, tail a journal, check zpool status, …).

Safety model (madde 309): an allow-list, nothing else. `proxmox_mcp.read_allow`
holds the data and the one validator (program -> read subcommands -> exact
options, operand/value rules, `pct exec <vmid> -- <cmd>` re-validated, no shell
operators at all). The same file is shared byte-for-byte with homelab-agent-sys,
whose second layer runs it too. The command that runs is rebuilt from the parsed
words, so what was checked is what bash sees.

No confirm / data-loss ack is needed — the command cannot mutate. Every call is
still recorded in the same host audit log.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from proxmox_mcp import config, host_ssh, read_allow
from proxmox_mcp.format import truncate
from proxmox_mcp.mcp_instance import mcp


def validate_read_command(command: str) -> Optional[str]:
    """Return None if `command` is an allow-listed read-only host command, else
    the rejection message. Pure function — unit-testable."""
    err = read_allow.validate(command)
    if err is None:
        return None
    return (f"Refused: {err} This tool runs one allow-listed read-only command "
            "(no pipes, redirects, chaining or substitution); for anything else "
            "use proxmox_host_exec (with confirm).")


class HostReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(
        ...,
        description=(
            "A single READ-ONLY command to run on the Proxmox host. Allow-list "
            "only: listed programs (cat, head, tail, ls, stat, du, df, grep, "
            "journalctl, ps, ss, free, smartctl, …), their read subcommands (zpool "
            "status/list, zfs list/get, systemctl status/show/is-active, pct/qm "
            "config/list/status, …) and their listed options by exact name (no "
            "abbreviations; an unknown option is refused). To read INSIDE a "
            "container use `pct exec <vmid> -- <read-only command>`; the inner "
            "command is checked the same way. No pipes, redirects, chaining or "
            "substitution (quoted text may contain any of these characters) — "
            "for those use proxmox_host_exec. An unquoted glob is allowed only in "
            "a path/operand of the host command, never at the start of a word; "
            "quote option values like --include='*.py' (and globs inside pct exec)."
        ),
        min_length=1, max_length=4096,
    )
    timeout: float = Field(
        default=30.0, description="Per-call timeout in seconds.", ge=1.0, le=120.0,
    )
    max_chars: int = Field(
        default=20000,
        description="Hard cap on returned output characters.",
        ge=500, le=200000,
    )
    reason: Optional[str] = Field(default=None, max_length=200)


@mcp.tool(
    name="proxmox_host_read_exec",
    annotations={
        "title": "Read-only Shell Command on Proxmox Host",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def proxmox_host_read_exec(params: HostReadInput) -> str:
    """Run a single ALLOW-LISTED, read-only command on the Proxmox host.

    Safe counterpart to proxmox_host_exec: it cannot mutate anything, so no
    confirm is required and it can be used by read-only agents for host
    diagnostics — e.g. `cat /etc/pve/...`, `journalctl -u pve -n 200`,
    `zpool status`, `systemctl status pvedaemon`.

    Rejects anything outside the allow-list (program, subcommand, option by
    exact name) and every shell operator (pipes, redirects, `;`, `$()`,
    backticks, subshells). For arbitrary commands (pipes, writes, ad-hoc work)
    use proxmox_host_exec, which requires confirm.
    """
    cfg = config.require_ssh()
    if cfg:
        return cfg

    err = validate_read_command(params.command)
    if err:
        host_ssh.audit_log(params.command, None, note=f"read-exec REFUSED: {err[:120]}")
        return err
    # Run the command rebuilt from the validated words, not the raw string.
    run = read_allow.check(params.command)[1]
    assert run is not None  # validated just above

    try:
        rc, stdout, stderr = await host_ssh.exec_command(run, timeout=params.timeout)
    except Exception as exc:
        host_ssh.audit_log(
            params.command, None, note=f"FAILED read-exec: {type(exc).__name__}: {exc}"
        )
        return host_ssh.format_host_ssh_error(exc)

    host_ssh.audit_log(
        params.command, rc,
        note="read-exec" + (f" reason={params.reason}" if params.reason else ""),
        stdout_preview=stdout[:200], stderr_preview=stderr[:200],
    )

    parts = [f"host=`{config.PROXMOX_SSH_HOST}`  rc={rc}"]
    if stdout.strip():
        parts.append("**stdout:**\n```\n" + truncate(stdout.rstrip(), params.max_chars) + "\n```")
    if stderr.strip():
        parts.append("**stderr:**\n```\n" + truncate(stderr.rstrip(), params.max_chars) + "\n```")
    if not stdout.strip() and not stderr.strip():
        parts.append("_(no output)_")
    return "\n\n".join(parts)
