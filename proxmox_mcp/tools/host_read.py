"""Read-only host shell exec (allow-listed).

`proxmox_host_exec` runs arbitrary commands and can destroy things, so it is
deliberately kept out of the read-only auditor subagent. This module adds a
*safe* alternative: `proxmox_host_read_exec` accepts only allow-listed,
non-mutating commands so it can be granted to a read-only agent for host
diagnostics (cat a config, tail a journal, check zpool status, …).

Safety model (defense in depth — all must pass):
  1. The raw command may not contain shell metacharacters that enable
     chaining / redirection / substitution / subshells (`;` `|` `&` `<` `>`
     `` ` `` `$` `(` `)` `{` `}` `!` `\\` and newlines). Globs (`*?[]`) and
     quotes are allowed — they only affect a single read command.
  2. The first token (binary basename) must be in the allow-list. For binaries
     with mutating subcommands (zpool, zfs, systemctl, pct, qm, pvesm, ip,
     pvesh) the first non-flag argument must be an allow-listed read subcommand.
  3. A few per-binary flags that would hang or change state are rejected
     (`-f`/`--follow` for tail/journalctl, `--rotate`/`--flush`/`--sync`/
     `--vacuum*` for journalctl, `-t`/`--test` for smartctl).
  4. As a final backstop the command is also run through the shared
     `host_ssh.is_destructive` detector and refused on any match.

No confirm / data-loss ack is needed — the command cannot mutate. Every call is
still recorded in the same host audit log.
"""
from __future__ import annotations

import shlex
from pathlib import PurePosixPath
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from proxmox_mcp import config, host_ssh
from proxmox_mcp.format import truncate
from proxmox_mcp.mcp_instance import mcp

# Characters that let a single command become several, redirect to a file, or
# substitute other command output. Globs (* ? [ ]) and quotes are intentionally
# NOT here — they only expand filenames for one read command.
_FORBIDDEN_CHARS = set(";|&<>`$(){}!\\\n\r")

# binary basename -> None (any args, read-only) | set of allowed read subcommands
_ANY = None
_READ_ALLOW: dict[str, Optional[set[str]]] = {
    # Pure read utilities — any arguments are non-mutating.
    "cat": _ANY, "head": _ANY, "tail": _ANY, "ls": _ANY, "stat": _ANY,
    "du": _ANY, "df": _ANY, "free": _ANY, "uptime": _ANY, "date": _ANY,
    "hostname": _ANY, "uname": _ANY, "lsblk": _ANY, "findmnt": _ANY,
    "wc": _ANY, "readlink": _ANY, "realpath": _ANY, "file": _ANY,
    "lscpu": _ANY, "getconf": _ANY, "ps": _ANY, "ss": _ANY, "sensors": _ANY,
    "pveversion": _ANY, "journalctl": _ANY, "smartctl": _ANY,
    "arcstat": _ANY, "arc_summary": _ANY,
    # Subcommand-restricted — the binary can also mutate.
    "zpool": {"status", "list", "get", "history", "iostat"},
    "zfs": {"list", "get"},
    "systemctl": {"status", "show", "is-active", "is-enabled", "is-failed",
                  "list-units", "list-timers", "list-unit-files", "cat"},
    "pct": {"config", "list", "status", "listsnapshot", "df"},
    "qm": {"config", "list", "status", "listsnapshot"},
    "pvesm": {"status", "list"},
    "ip": {"addr", "link", "route", "neigh", "a", "l", "r", "n"},
    "pvesh": {"get"},
}

# Per-binary flags that would hang the call or change state.
_FOLLOW_HANGS = {"tail", "journalctl"}
_JOURNAL_BAD_FLAGS = {"--rotate", "--flush", "--sync"}


def validate_read_command(command: str) -> Optional[str]:
    """Return None if `command` is a safe read-only host command, else an error
    string explaining the rejection. Pure function — unit-testable."""
    bad = _FORBIDDEN_CHARS.intersection(command)
    if bad:
        shown = " ".join(sorted(bad)).replace("\n", "\\n").replace("\r", "\\r")
        return (
            f"Refused: command contains disallowed shell character(s): {shown}. "
            "This tool runs a single read-only command — no pipes, redirects, "
            "substitution, or chaining. Use proxmox_host_exec (with confirm) for that."
        )
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return f"Refused: could not parse command ({exc})."
    if not argv:
        return "Refused: empty command."

    binary = PurePosixPath(argv[0]).name
    if binary not in _READ_ALLOW:
        return (
            f"Refused: '{binary}' is not in the read-only allow-list. "
            "Allowed: " + ", ".join(sorted(_READ_ALLOW)) + ". "
            "For anything else use proxmox_host_exec (with confirm)."
        )

    allowed_subs = _READ_ALLOW[binary]
    if allowed_subs is not None:
        sub = next((a for a in argv[1:] if not a.startswith("-")), None)
        if sub is None or sub not in allowed_subs:
            return (
                f"Refused: '{binary} {sub or ''}'.strip() is not a read-only "
                f"subcommand. Allowed for {binary}: "
                + ", ".join(sorted(allowed_subs)) + "."
            )

    if binary in _FOLLOW_HANGS and (
        "-f" in argv or "--follow" in argv
    ):
        return f"Refused: '{binary} -f/--follow' would block. Use -n <lines> instead."
    if binary == "journalctl":
        for a in argv[1:]:
            if a in _JOURNAL_BAD_FLAGS or a.startswith("--vacuum"):
                return f"Refused: journalctl '{a}' mutates the journal."
    if binary == "smartctl" and any(
        a in ("-t", "--test") or a.startswith("--test=") for a in argv
    ):
        return "Refused: 'smartctl -t/--test' starts a device self-test (changes state)."

    # Final backstop: shared destructive-pattern detector.
    danger = host_ssh.is_destructive(command)
    if danger:
        return f"Refused: command matches destructive pattern {danger!r}."
    return None


class HostReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(
        ...,
        description=(
            "A single READ-ONLY command to run on the Proxmox host. Allow-listed "
            "binaries only (cat, head, tail, ls, stat, du, df, journalctl, "
            "zpool status/list, zfs list/get, systemctl status/show, pct/qm "
            "config/list/status, smartctl, …). No pipes, redirects, chaining, or "
            "substitution — for those use proxmox_host_exec. Globs and quotes are "
            "allowed."
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

    Rejects anything outside the allow-list, any shell metacharacters (pipes,
    redirects, `;`, `$()`, backticks, subshells), and mutating flags. For
    arbitrary commands (pipes, writes, ad-hoc work) use proxmox_host_exec, which
    requires confirm.
    """
    cfg = config.require_ssh()
    if cfg:
        return cfg

    err = validate_read_command(params.command)
    if err:
        host_ssh.audit_log(params.command, None, note=f"read-exec REFUSED: {err[:120]}")
        return err

    try:
        rc, stdout, stderr = await host_ssh.exec_command(
            params.command, timeout=params.timeout
        )
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
