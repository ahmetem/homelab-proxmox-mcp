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
     with mutating subcommands (zpool, zfs, systemctl, pct, qm, pvesm, pvesh,
     apt-cache, nvme…) the first non-flag argument must be an allow-listed
     read subcommand, and only value-less flags may precede it. `ip` must be
     `ip [read options] OBJECT [show|list|ls|lst]` (route/neigh `get` too).
  3. Binaries that can also write or change state (date, hostname, tree, ss,
     arcstat, smartctl, journalctl, sensors, lshw, file, apt-cache, zdb) have
     an option allow-list (`_OPTS`): an option not listed as a read form is
     refused (fail-closed). dmesg/dmidecode/nvme/coredumpctl keep a deny-list
     of their write/state flags; `tail -f` would block.
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

import re
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
    "dpkg-query": _ANY,

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
    "ip": _ANY,  # argv checked by _check_ip: OBJECT + show/list only
    "apt-cache": {"show", "showpkg", "showsrc", "stats", "dump", "dumpavail",
                  "unmet", "search", "depends", "rdepends", "pkgnames", "policy",
                  "madison"},  # not gencaches (writes the cache)
    "pvesh": {"get"},
    "proxmox-boot-tool": {"status"},
    "nvme": {"list", "list-subsys", "smart-log", "id-ctrl", "id-ns",
             "list-ns", "ns-descs", "error-log", "fw-log", "get-feature",
             "get-log", "show-regs"},  # not telemetry-log (writes a file)
    "apt": {"list"},
    "apt-mark": {"showhold", "showmanual", "showauto", "showinstall"},
    "chronyc": {"tracking", "sources", "sourcestats", "activity",
                "ntpdata", "serverstats", "selectdata", "clients"},
    "coredumpctl": {"list", "info"},
}

# Per-binary flags that would hang the call, write a file, or change state.
# Any argv token that exactly matches, or (for '=' forms) starts with, one of
# these is refused. A deny-list: only for binaries whose every write/state
# option is listed here; the rest of the state-capable binaries have an
# option allow-list in _OPTS below.
_FOLLOW_HANGS = {"tail"}
_DENIED_FLAGS: dict[str, set[str]] = {
    # dmesg can clear the ring buffer, toggle the console, set log level, follow.
    "dmesg": {"-C", "--clear", "-c", "--read-clear", "-D", "--console-off",
              "-E", "--console-on", "-n", "--console-level", "-w", "--follow",
              "-W", "--follow-new"},
    # dmidecode --dump-bin FILE / --dump write SMBIOS to a file.
    "dmidecode": {"--dump-bin", "--dump"},
    # write a telemetry log / a core to a file
    "nvme": {"--output-file", "-O"},
    "coredumpctl": {"-o", "--output"},
}

# Option allow-lists (fail-closed, madde 312): for a binary listed here every
# option must be a known read form, anything else is refused. Fields: short
# flags, short options taking a value, long flags (`name=`: also `--name=X`),
# long options taking a value, short options with an optional value that is a
# separate word only when it matches the regex (journalctl `-n 50`, `-b -1`;
# None: attached only, `date -Iseconds`). From the binaries' --help / man
# pages (coreutils 9, iproute2 6, systemd 257, smartmontools 7, util-linux).
# Left out on purpose: `date -s/--set`, `hostname NAME/-F/-b`, `tree -o/-R`,
# `ss -K/-D`, `arcstat -o`, `smartctl -s/-o/-S/-t/-X/-C/--set`, `journalctl
# --setup-keys/--update-catalog/--rotate/--flush/--sync/--vacuum-*/
# --relinquish-var/--cursor-file/-f`, `sensors -s`, `lshw -dump`, `file -C/-p`,
# `apt-cache -p/-s/-o/-c`, `zdb -e/-F/-X/-U/-x/-R`.
_OPTS: dict[str, tuple] = {
    "date": ("uR", "dfr", "utc universal rfc-email debug help version resolution iso-8601=",
             "date file reference rfc-3339", {"I": None}),
    "hostname": ("sfdiIAayVh", "", "short fqdn long domain ip-address all-ip-addresses "
                 "all-fqdns alias yp nis help version", "", {}),
    "tree": ("adlfxpugshDFvtcUrinACSXJqNQ", "LPI", "gitignore ignore-case matchdirs "
             "metafirst prune info noreport du si inodes device dirsfirst filesfirst "
             "fromfile nolinks help version", "charset filelimit timefmt sort", {}),
    "ss": ("hVHOnrapolemiTstuwxdSb46MZzE0", "fANF", "help version no-header oneline "
           "numeric resolve all listening options extended memory info processes "
           "threads summary tos cgroup tipcinfo context contexts tcp udp sctp unix raw "
           "dccp xdp packet vsock tipc mptcp ipv4 ipv6 bpf events inet-sockopt",
           "family query socket net filter", {}),
    "arcstat": ("ahpvxz", "fs", "", "", {}),
    "smartctl": ("hViaxcAHj", "glqdTbrnFvPfB", "help version info all xall scan "
                 "scan-open capabilities attributes health json= identify=",
                 "get log quietmode device tolerance badsum report nocheck firmwarebug "
                 "vendorattribute presets format drivedb", {}),
    "journalctl": ("rxaqlemNkhV", "utTpgSUoDMFci", "reverse catalog all quiet full "
                   "no-full pager-end merge dmesg fields list-boots disk-usage header "
                   "verify utc show-cursor system user no-pager no-hostname list-catalog "
                   "dump-catalog list-namespaces help version boot= lines= "
                   "case-sensitive=",
                   "unit user-unit identifier exclude-identifier priority grep since "
                   "until output output-fields directory file root image machine field "
                   "cursor after-cursor facility namespace verify-key",
                   {"n": r"[-+]?\d+|all", "b": r"[-+]?\d+|all|[0-9a-fA-F]{32}([-+]\d+)?"}),
    "sensors": ("Aujfhv", "c", "no-adapter bus-list help version", "config-file", {}),
    # lshw's long options take ONE dash (`-short`, `-class disk`)
    "lshw": ("", "", "version help X html xml json short businfo quiet sanitize "
             "numeric notime", "class C c disable enable", {}),
    "file": ("bcdEhiklLNnrsSvz0Z", "efFmP", "brief checking-printout debug dereference "
             "no-dereference keep-going list mime mime-type mime-encoding no-buffer "
             "no-pad raw special-files no-sandbox uncompress uncompress-noreport print0 "
             "apple extension help version",
             "exclude exclude-quiet files-from separator magic-file parameter", {}),
    "apt-cache": ("qiafn", "", "quiet important full all-versions no-all-versions "
                  "all-names recurse installed names-only no-pre-depends no-depends "
                  "no-recommends no-suggests no-conflicts no-breaks no-replaces "
                  "no-enhances help version", "", {}),
    "zdb": ("bcCdDhilLmMPsSuvAG", "", "", "", {}),
}
_SINGLE_DASH_LONG = {"lshw"}

# Flags allowed BEFORE the subcommand of a subcommand-restricted binary. An
# option there that takes a separate value could swallow the word we take
# for the subcommand (`systemctl --state status stop x` stops x), so only
# value-less flags and attached `--opt=value` forms are accepted.
_PRE_SUB_FLAGS: dict[str, set[str]] = {
    "systemctl": {"--no-pager", "--no-legend", "--plain", "--full", "-l", "--all",
                  "-a", "--user", "--system", "--failed", "-q", "--quiet"},
    "chronyc": {"-n", "-c", "-4", "-6"},
    "coredumpctl": {"--no-pager", "--no-legend", "-r", "--reverse", "-q", "--quiet"},
}

# `ip [OPTIONS] OBJECT [show|list|…]` — only reads. A missing action is a show;
# `get` is a lookup for route/neigh. `ip -b/-batch FILE`, `-force` and every
# other action (set, add, del, flush, exec…) are refused. Exact words only:
# ip takes prefixes, and `ip link s` is `set`.
_IP_FLAGS = {"-s", "-stats", "-statistics", "-d", "-details", "-br", "-brief", "-c",
             "-color", "-4", "-6", "-0", "-j", "-json", "-p", "-pretty", "-o",
             "-oneline", "-h", "-human", "-human-readable", "-a", "-all", "-t",
             "-timestamp", "-r", "-resolve", "-iec", "-V", "-Version"}
_IP_VALUE_OPTS = {"-f", "-family", "-n", "-netns"}
_IP_OBJECTS = {"address", "addr", "a", "link", "l", "route", "r", "ro", "neighbour",
               "neighbor", "neigh", "n", "rule", "ru", "maddress", "maddr", "m",
               "netconf", "netns"}
_IP_READ = {"show", "list", "lst", "ls"}


def _check_ip(args: list[str]) -> Optional[str]:
    k = 0
    while k < len(args) and args[k].startswith("-"):
        a = args[k]
        k += 1
        if a in _IP_VALUE_OPTS:
            k += 1
        elif a not in _IP_FLAGS and a.split("=", 1)[0] not in ("-c", "-color"):
            return f"Refused: 'ip {a}' is not a read-only ip option."
    obj = args[k] if k < len(args) else None
    if obj not in _IP_OBJECTS:
        return (f"Refused: 'ip {obj or ''}' is not a readable ip object. "
                "Allowed: " + ", ".join(sorted(_IP_OBJECTS)) + ".")
    action = args[k + 1] if k + 1 < len(args) else "show"
    if action in _IP_READ or (action == "get" and obj in ("route", "r", "ro",
                                                         "neighbour", "neighbor", "neigh", "n")):
        return None
    return (f"Refused: 'ip {obj} {action}' is not read-only. Only "
            + "/".join(sorted(_IP_READ)) + " (and route/neigh get) are allowed.")


def _check_options(binary: str, args: list[str]) -> Optional[str]:
    """Refuse any option of `binary` that _OPTS doesn't list as a read form."""
    sflags, svals, lflags, lvals, sopt = _OPTS[binary]
    lvals = set(lvals.split())
    longs = {f.rstrip("=") for f in lflags.split()}
    long_eq = {f[:-1] for f in lflags.split() if f.endswith("=")} | lvals
    single = binary in _SINGLE_DASH_LONG
    operands, values = [], []
    k = 0

    def no(opt: str) -> str:
        return (f"Refused: '{binary} {opt}' is not a known read-only option of "
                f"{binary}; this tool allows only read forms.")

    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            operands += args[k:]
            break
        if a.startswith("--") or (single and a.startswith("-") and len(a) > 1):
            key, eq, val = a.lstrip("-").partition("=")
            if key in lvals and not eq:
                val = args[k] if k < len(args) else ""
                k += 1
            elif key not in longs and key not in lvals or eq and key not in long_eq:
                return no(a)
            values.append((key, val))
        elif a.startswith("-") and a != "-":
            for p, ch in enumerate(a[1:], 2):
                if ch in svals:
                    val = a[p:] if p < len(a) else (args[k] if k < len(args) else "")
                    k += p == len(a)
                    values.append((ch, val))
                    break
                if ch in sopt:
                    rx = sopt[ch]
                    if p == len(a) and rx and k < len(args) and re.fullmatch(rx, args[k]):
                        k += 1
                    break
                if ch not in sflags:
                    return no(a)
        else:
            operands.append(a)
    if binary == "date" and any(not o.startswith("+") for o in operands):
        return "Refused: 'date' with an operand other than +FORMAT sets the clock."
    if binary == "hostname" and operands:
        return "Refused: 'hostname NAME' sets the hostname."
    if binary == "smartctl" and any(o in ("l", "log") and v.startswith("scterc,")
                                    for o, v in values):
        return "Refused: 'smartctl -l scterc,R,W' sets the error recovery timer."
    return None


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
    if binary == "ip":
        err = _check_ip(argv[1:])
        if err:
            return err
    elif allowed_subs is not None:
        at = next((j for j, a in enumerate(argv[1:], 1) if not a.startswith("-")), None)
        sub = argv[at] if at else ("list-units" if binary == "systemctl" else None)
        pre = [a for a in argv[1:at or len(argv)]
               if "=" not in a and a not in _PRE_SUB_FLAGS.get(binary, ())]
        if pre and binary not in _OPTS:
            return (f"Refused: '{binary} {pre[0]}' before the subcommand: only "
                    "value-less flags or --opt=value may come first (a separate "
                    "value could hide the real subcommand).")
        if sub is None or sub not in allowed_subs:
            return (
                f"Refused: '{binary} {sub}' is not a read-only "
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
    if binary in _OPTS:
        err = _check_options(binary, argv[1:])
        if err:
            return err

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
