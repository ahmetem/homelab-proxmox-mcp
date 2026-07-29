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
  5. `pct exec <vmid> -- <cmd>` is the ONE way to read inside a container.
     The part after `--` is re-validated by this same function, so the
     container gets exactly the host allow-list -- it is not a bypass.
     One level only (no nested `pct exec`).

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
    # --- Pure read utilities — any arguments are non-mutating. ---
    # Files / text (no in-place edit or write option on any of these).
    "cat": _ANY, "head": _ANY, "tail": _ANY, "ls": _ANY, "stat": _ANY,
    "du": _ANY, "df": _ANY, "wc": _ANY, "readlink": _ANY, "realpath": _ANY,
    "file": _ANY, "findmnt": _ANY, "getfacl": _ANY, "nl": _ANY, "tac": _ANY,
    "tree": _ANY, "grep": _ANY, "egrep": _ANY, "zgrep": _ANY, "zcat": _ANY,
    # System / hardware info.
    "free": _ANY, "uptime": _ANY, "date": _ANY, "hostname": _ANY,
    "uname": _ANY, "arch": _ANY, "nproc": _ANY, "lscpu": _ANY,
    "getconf": _ANY, "lsblk": _ANY, "lsmod": _ANY, "lspci": _ANY,
    "lsusb": _ANY, "lsscsi": _ANY, "lshw": _ANY, "sensors": _ANY,
    "dmidecode": _ANY,  # write flag --dump-bin is blocked in _DENIED_FLAGS
    "dmesg": _ANY,      # clear/console/-follow flags blocked in _DENIED_FLAGS
    # Processes / network / users.
    "ps": _ANY, "ss": _ANY, "lsof": _ANY, "pgrep": _ANY, "pidof": _ANY,
    "vmstat": _ANY, "iostat": _ANY, "mpstat": _ANY, "numastat": _ANY,
    "who": _ANY, "w": _ANY, "last": _ANY, "id": _ANY, "groups": _ANY,
    "whoami": _ANY, "getent": _ANY,
    # LVM reporting (mutators are lvcreate/lvremove/pvcreate/… — separate bins).
    "pvs": _ANY, "lvs": _ANY, "vgs": _ANY,
    "pvdisplay": _ANY, "lvdisplay": _ANY, "vgdisplay": _ANY,
    # ZFS / storage read tools.
    "arcstat": _ANY, "arc_summary": _ANY, "zdb": _ANY,
    # Proxmox / packages.
    "pveversion": _ANY, "journalctl": _ANY, "smartctl": _ANY,
    "dpkg-query": _ANY, "apt-cache": _ANY,

    # --- Subcommand-restricted — the binary can also mutate. ---
    "zpool": {"status", "list", "get", "history", "iostat", "version"},
    "zfs": {"list", "get", "version", "holds"},
    "systemctl": {"status", "show", "is-active", "is-enabled", "is-failed",
                  "list-units", "list-timers", "list-unit-files", "cat",
                  "list-jobs", "list-dependencies", "list-sockets",
                  "get-default"},
    # "exec" is accepted ONLY in the `pct exec <vmid> -- <cmd>` form, and the
    # inner command is re-validated against this very allow-list (see
    # _validate_pct_exec). Without that recursion this entry would be a hole.
    "pct": {"config", "list", "status", "listsnapshot", "df", "pending", "exec"},
    "qm": {"config", "list", "status", "listsnapshot", "pending", "showcmd"},
    "pvesm": {"status", "list", "path", "apiinfo"},
    "ip": {"addr", "link", "route", "neigh", "a", "l", "r", "n", "-s"},
    "pvesh": {"get"},
    "proxmox-boot-tool": {"status"},
    "nvme": {"list", "list-subsys", "smart-log", "id-ctrl", "id-ns",
             "list-ns", "ns-descs", "error-log", "fw-log", "get-feature",
             "get-log", "show-regs", "telemetry-log"},
    "apt": {"list"},
    "apt-mark": {"showhold", "showmanual", "showauto", "showinstall"},
    "chronyc": {"tracking", "sources", "sourcestats", "activity",
                "ntpdata", "serverstats", "selectdata", "clients"},
    "coredumpctl": {"list", "info"},
}

# Per-binary flags that would hang the call, write a file, or change state.
# Any argv token that exactly matches, or (for '=' forms) starts with, one of
# these is refused. Covers the write/state escape hatches on otherwise-read
# binaries listed as _ANY above.
_FOLLOW_HANGS = {"tail", "journalctl"}
_JOURNAL_BAD_FLAGS = {"--rotate", "--flush", "--sync"}
_DENIED_FLAGS: dict[str, set[str]] = {
    # dmesg can clear the ring buffer, toggle the console, set log level, follow.
    "dmesg": {"-C", "--clear", "-c", "--read-clear", "-D", "--console-off",
              "-E", "--console-on", "-n", "--console-level", "-w", "--follow",
              "-W", "--follow-new"},
    # dmidecode --dump-bin FILE / --dump write SMBIOS to a file.
    "dmidecode": {"--dump-bin", "--dump"},
}


# `pct exec` runs a command INSIDE a container. There was no read-only way to
# look inside a CT at all (proxmox_host_read_exec only covers the host, and
# proxmox_lxc_exec is S5 in the agent's safety model), so diagnosing "what does
# this service see from inside" was impossible without a forbidden tool. The
# opening is deliberately narrow: the inner command is re-validated by this same
# function, so exactly the host allow-list applies inside the container too.
# Nesting is pointless and confusing, so one level only.
_PCT_EXEC_MAX_DEPTH = 1


def _validate_pct_exec(argv: list[str], depth: int) -> Optional[str]:
    """Validate `pct exec <vmid> -- <read-only command>`. Returns None if the
    inner command is allow-listed, else the rejection reason."""
    if depth >= _PCT_EXEC_MAX_DEPTH:
        return "Refused: nested 'pct exec' is not allowed."
    if "--" not in argv:
        return (
            "Refused: 'pct exec' must be written "
            "`pct exec <vmid> -- <read-only command>`. The '--' separator is "
            "required so the inner command can be validated on its own."
        )
    cut = argv.index("--")
    head, inner = argv[:cut], argv[cut + 1:]
    vmid = next((a for a in head[2:] if not a.startswith("-")), None)
    if vmid is None or not vmid.isdigit():
        return "Refused: 'pct exec' needs a numeric <vmid> before '--'."
    if not inner:
        return "Refused: 'pct exec' has no command after '--'."
    err = validate_read_command(shlex.join(inner), _depth=depth + 1)
    if err:
        # Make it obvious the rejection is about the INNER command.
        return err.replace("Refused:", "Refused (inside CT %s):" % vmid, 1)
    return None


def validate_read_command(command: str, _depth: int = 0) -> Optional[str]:
    """Return None if `command` is a safe read-only host command, else an error
    string explaining the rejection. Pure function — unit-testable.

    `_depth` is internal: it bounds the `pct exec` recursion."""
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
        if binary == "pct" and sub == "exec":
            err = _validate_pct_exec(argv, _depth)
            if err:
                return err
            # Fall through: the shared backstops below (denied flags, and above
            # all host_ssh.is_destructive on the WHOLE command string) still run.

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

    denied = _DENIED_FLAGS.get(binary)
    if denied:
        for a in argv[1:]:
            if a in denied or a.split("=", 1)[0] in denied:
                return (
                    f"Refused: '{binary} {a}' can clear/write state or block; "
                    "not allowed in read-only mode."
                )

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
            "config/list/status, smartctl, …). To read INSIDE a container use "
            "`pct exec <vmid> -- <read-only command>`; the inner command must "
            "itself be allow-listed. No pipes, redirects, chaining, or "
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
