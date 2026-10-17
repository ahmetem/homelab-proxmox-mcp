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


def _refusal(err: str) -> str:
    return (f"Refused: {err} This tool runs one allow-listed read-only command "
            "(no pipes, redirects, chaining or substitution); for anything else "
            "use proxmox_host_exec (with confirm).")


def validate_read_command(command: str) -> Optional[str]:
    """Return None if `command` is an allow-listed read-only host command, else
    the rejection message. Pure function — unit-testable."""
    err = read_allow.validate(command)
    return None if err is None else _refusal(err)


# Bounds on what the allow-list cannot see (madde 325): `timeout` (read_allow.check)
# ends a big interval or a blocking read; RLIMIT_DATA stops `tail /dev/zero`, which
# buffers a line that never ends (~3 GB/s); `head -c` stops `cat /dev/zero` at the
# output cap instead of streaming it into this process. Host login shell is bash.
DATA_LIMIT_KB = 1024 * 1024


def remote_command(run: str) -> str:
    return (f"ulimit -d {DATA_LIMIT_KB}; {run} | head -c {host_ssh.MAX_OUTPUT_BYTES + 1}; "
            "exit ${PIPESTATUS[0]}")


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
        default=30.0, ge=1.0, le=120.0,
        description="Seconds the command may run; then it is stopped on the host (and "
                    "inside the CT for pct exec) and its output so far returned.",
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

    # Run the command rebuilt from the validated words, not the raw string.
    err, run = read_allow.check(params.command, timeout=params.timeout)
    if err or run is None:
        err = _refusal(err or "")
        host_ssh.audit_log(params.command, None, note=f"read-exec REFUSED: {err[:120]}")
        return err

    try:
        rc, stdout, stderr = await host_ssh.exec_command(
            remote_command(run), timeout=params.timeout + read_allow.KILL_AFTER + 5)
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
    if rc in (124, 137):
        parts.append(f"_(stopped by the {params.timeout:g}s timeout)_")
    elif rc == 141:
        parts.append(f"_(stopped at the {host_ssh.MAX_OUTPUT_BYTES} byte output cap)_")
    if stdout.strip():
        parts.append("**stdout:**\n```\n" + truncate(stdout.rstrip(), params.max_chars) + "\n```")
    if stderr.strip():
        parts.append("**stderr:**\n```\n" + truncate(stderr.rstrip(), params.max_chars) + "\n```")
    if not stdout.strip() and not stderr.strip():
        parts.append("_(no output)_")
    return "\n\n".join(parts)
