"""Free-exec SSH client for the Proxmox HOST (not guest VMs).

This sits ALONGSIDE proxmox_mcp/ssh.py (which has an allow-list for safer
operations like wipefs/zfs/zpool). The user explicitly opted into
unrestricted command execution on the Proxmox host itself.

Mirrors the design of proxmox_mcp/vm_ssh.py:
  - no binary allow-list
  - confirm=true on every exec
  - destructive-pattern detector -> requires i_understand_data_loss=true
  - every call written to _host_ssh_audit.log

Connection details come from the existing PROXMOX_SSH_* env vars in .env.
"""
from __future__ import annotations

import base64
import datetime as _dt
import os
import re
import shlex
from pathlib import Path
from typing import Optional

try:
    import asyncssh  # type: ignore
except ImportError:
    asyncssh = None  # type: ignore

from proxmox_mcp import audit, config
from proxmox_mcp.redact import redact


# Same cap as the other SSH modules
MAX_OUTPUT_BYTES = 1_048_576

# Audit log next to the package root
_AUDIT_PATH = Path(__file__).resolve().parent.parent / "_host_ssh_audit.log"


# Same regex set as vm_ssh.py — patterns that look destructive enough to
# require explicit ack. On a Proxmox host these are MORE dangerous than
# on a guest VM, so the detector is the same (in addition to a few host-
# specific patterns: zpool destroy, qm destroy, pvesm remove, etc.).
_DESTRUCTIVE_PATTERNS = [
    re.compile(r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r", re.IGNORECASE),
    re.compile(r"\brm\s+-rf?\s+/", re.IGNORECASE),
    re.compile(r"\bmkfs\b", re.IGNORECASE),
    re.compile(r"\bdd\s+[^|]*\bof=/dev/", re.IGNORECASE),
    re.compile(r">\s*/dev/(sd|nvme|vd|hd)", re.IGNORECASE),
    re.compile(r"\bshred\b", re.IGNORECASE),
    re.compile(r"\bwipefs\b", re.IGNORECASE),
    re.compile(r"\bparted\b.*\b(mklabel|rm)\b", re.IGNORECASE),
    re.compile(r":\(\)\s*\{.*\}\s*;\s*:", re.DOTALL),  # fork bomb
    re.compile(r"\bshutdown\b|\bhalt\b|\bpoweroff\b|\breboot\b", re.IGNORECASE),
    re.compile(r"\bchmod\s+(-R\s+)?[0-7]*[0-7][0-7][0-7]\s+/", re.IGNORECASE),
    re.compile(r"\bchown\s+-R\b.*\s+/\b", re.IGNORECASE),
    # Proxmox-host-specific destructive patterns
    re.compile(r"\bzpool\s+destroy\b", re.IGNORECASE),
    re.compile(r"\bzfs\s+destroy\b", re.IGNORECASE),
    re.compile(r"\bqm\s+(destroy|stop)\b", re.IGNORECASE),
    re.compile(r"\bpct\s+(destroy|stop)\b", re.IGNORECASE),
    re.compile(r"\bpvesm\s+remove\b", re.IGNORECASE),
    re.compile(r"\bsystemctl\s+(stop|disable|mask)\b.*\b(pve|qemu|corosync|pvedaemon)\b", re.IGNORECASE),
]


class HostSshError(Exception):
    def __init__(self, message: str, *, rc: Optional[int] = None,
                 stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.message = message
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr


# Wrappers that run content the patterns above can't see: a decoder, eval,
# a pipe into a shell or interpreter, a substituted or process-substituted
# script. Fail closed: they need the ack whatever the payload holds
# (`base64 -d | sh` of a harmless line too). Shared with vm_ssh.
#
# They are judged on the command's simple commands, not on the raw text: a
# small shell scanner splits on unquoted `; & | || && ( ) \n` and backticks,
# strips quotes and escapes, and follows `$(…)`/`<(…)` (inside double quotes
# too), `<shell> … -c '<script>'`, `ssh host <cmd>` and `pct exec N -- <cmd>`.
# Every simple command's *head* is then found one way (`_head`): keywords,
# `VAR=` assignments and wrapper chains (sudo/env/xargs/timeout…) skipped.
# So `false || eval "$X"` is caught while `grep -E 'a|eval'` is not.
# kisayol: a scanner, not a shell. Ceiling: payloads built from variables or
# `$'\x72m'`/printf escapes, `env -S '<cmd>'`, a script string handed to
# anything but a shell/su/runuser `-c`, ssh or pct/qm/lxc-attach/runuser `--`
# unless it starts with `eval` (quote-eval backstop: `trap`, `parallel`…),
# `| su user` (its operand is a user), interpreter flags read as "program
# given" (`| python3 -E`), `$( (…) )` closing early. Upgrade path: a real
# shell parser (bashlex, a new dependency) or an allow-list like host_read.
_SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "ash", "mksh"}
# hand their `-c` string to a shell; `runuser -u x -- cmd` runs cmd as argv
_SCRIPT_RUNNERS = _SHELLS | {"su", "runuser"}
_INTERP = re.compile(r"(python|pypy)[0-9.]*|perl|ruby|node(js)?|php[0-9.]*")
# wrapper -> (short options that take a value, positional args before the command)
_WRAPPERS = {
    "sudo": ("ugCDhprtU", 0), "doas": ("uC", 0), "env": ("uCS", 0),
    "xargs": ("IdEnLPsa", 0), "nice": ("n", 0), "timeout": ("sk", 1),
    "stdbuf": ("ioe", 0), "chroot": ("", 1), "nohup": ("", 0), "exec": ("a", 0),
    "command": ("", 0), "builtin": ("", 0), "time": ("fo", 0), "setsid": ("", 0),
}
_KEYWORDS = {"if", "then", "do", "else", "elif", "while", "until", "!", "{", "}"}
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# Decode calls inside an interpreter's program (python -c, perl -e, heredoc).
# Only when an interpreter is a command head, so `grep -rn b64decode` passes.
_DECODE_CALL = re.compile(r"\b(b64decode|decodebytes|a2b_base64|decode_base64|decode64|uudecode)\b")
_MAX_NEST = 20


class _TooDeep(Exception):
    pass


def _scan(s: str, out: list, depth: int = 0, i: int = 0, close: str = "") -> int:
    """Append (piped, words) for each simple command in `s` to `out`.

    Words have quotes and escapes removed; a `$(…)`, backtick or `<(…)` leaves
    the marker `$()`/`<()` in its word and its own commands in `out`. Returns
    the index after `close` (or len(s))."""
    if depth > _MAX_NEST:
        raise _TooDeep
    words: list = []
    word: list = []
    has_word = False
    piped = False
    n = len(s)

    def end_word():
        nonlocal word, has_word
        if has_word:
            words.append("".join(word))
        word, has_word = [], False

    def end_cmd(next_piped: bool):
        nonlocal words, piped
        end_word()
        if words:
            out.append((piped, words))
            words, piped = [], next_piped
        else:  # `a | (sh)`, `a |<newline> sh`: the pipe still feeds the next command
            piped = piped or next_piped

    def subst(j: int, marker: str) -> int:  # j: index after the opening `(`
        nonlocal has_word
        word.append(marker)
        has_word = True
        return _scan(s, out, depth + 1, j, ")")

    def backtick(j: int) -> int:  # j: index after the opening backtick
        nonlocal has_word
        k = s.find("`", j)
        k = n if k < 0 else k
        _scan(s[j:k], out, depth + 1)
        word.append("$()")
        has_word = True
        return k + 1

    while i < n:
        c = s[i]
        if close and c == close:
            end_cmd(False)
            return i + 1
        if c == "\\":
            if s[i + 1:i + 2] != "\n":
                word.append(s[i + 1:i + 2])
                has_word = True
            i += 2
        elif c == "'":
            k = s.find("'", i + 1)
            k = n if k < 0 else k
            word.append(s[i + 1:k])
            has_word = True
            i = k + 1
        elif c == '"':
            has_word = True
            i += 1
            while i < n and s[i] != '"':
                if s[i] == "\\" and s[i + 1:i + 2] in ('"', "\\", "$", "`", "\n"):
                    word.append(s[i + 1])
                    i += 2
                elif s.startswith("$(", i):
                    i = subst(i + 2, "$()")
                elif s[i] == "`":
                    i = backtick(i + 1)
                else:
                    word.append(s[i])
                    i += 1
            i += 1
        elif c == "$" and s.startswith("$(", i):
            i = subst(i + 2, "$()")
        elif c in "<>" and s.startswith("(", i + 1):
            i = subst(i + 2, "<()")
        elif c == "`":
            i = backtick(i + 1)
        elif c == "#" and not has_word:
            k = s.find("\n", i)
            i = n if k < 0 else k
        elif c == "&" and (s[i + 1:i + 2] == ">" or (word and word[-1].endswith((">", "<")))):
            word.append(c)  # redirection: `&>f`, `2>&1`
            has_word = True
            i += 1
        elif c in ";&|\n()":
            pipe = c == "|" and s[i + 1:i + 2] != "|"
            end_cmd(pipe)
            i += 2 if c in "&|" and s[i + 1:i + 2] in (c, "&") else 1
        elif c in " \t\r":
            end_word()
            i += 1
        else:
            word.append(c)
            has_word = True
            i += 1
    end_cmd(False)
    return n


def _head(words: list) -> tuple:
    """(command basename, its args) after keywords, assignments and wrappers.

    The single definition of "command position" every opaque check uses."""
    k = 0
    while k < len(words):
        w = words[k]
        if w in _KEYWORDS or _ASSIGN.match(w):
            k += 1
            continue
        spec = _WRAPPERS.get(w.rsplit("/", 1)[-1])
        if spec is None:
            break
        takes, positional = spec
        k += 1
        while k < len(words) and words[k].startswith("-") and words[k] != "-":
            opt = words[k]
            k += 1
            if opt == "--":
                break
            if w.endswith("command") and opt in ("-v", "-V"):
                return "", []  # `command -v eval` looks a name up
            if len(opt) == 2 and opt[1] in takes:
                k += 1
        k += positional
    if k >= len(words):
        return "", []
    return words[k].rsplit("/", 1)[-1], words[k + 1:]


def _scripts(words: list):
    """Strings in one simple command that another shell will parse."""
    name, args = _head(words)
    shell_at = next((j for j, w in enumerate(words)
                     if w.rsplit("/", 1)[-1] in _SCRIPT_RUNNERS), None)
    if shell_at is not None:
        for j in range(shell_at + 1, len(words) - 1):
            if words[j] == "--":  # `runuser -u x -- psql -c …`: not the shell's -c
                break
            if re.fullmatch(r"-[A-Za-z]*c", words[j]):
                yield words[j + 1]
                break
    if name == "ssh":  # ssh joins its args with spaces for the remote shell
        j = 0
        while j < len(args) and args[j].startswith("-"):
            j += 2 if len(args[j]) == 2 and args[j][1] in "bcDEeFIiJLlmOopQRSWw" else 1
        if j + 1 < len(args):
            yield " ".join(args[j + 1:])
    if name in ("pct", "qm", "lxc-attach", "runuser") and "--" in args:
        yield shlex.join(args[args.index("--") + 1:])


def _commands(cmd: str) -> list:
    """Every simple command in `cmd`, nested scripts included."""
    out: list = []
    _scan(cmd, out)
    j = 0
    while j < len(out):  # each script is shorter than its parent: terminates
        for script in _scripts(out[j][1]):
            _scan(script, out)
        j += 1
    return out


def _opaque(cmd: str, cmds: list) -> Optional[str]:
    """Name the first opaque construct among the simple commands, or None."""
    interp = False
    for piped, words in cmds:
        name, args = _head(words)
        if not name:
            continue
        shell, is_interp = name in _SHELLS, bool(_INTERP.fullmatch(name))
        interp = interp or is_interp
        if name in ("eval", "b64decode", "uudecode"):
            return name
        if name in ("base64", "base32") and any(
                a == "--decode" or re.fullmatch(r"-[a-zA-Z]*[dD][a-zA-Z]*", a) for a in args):
            return f"{name} -d"
        if name == "openssl" and args[:1] in (["enc"], ["base64"]) and "-d" in args:
            return f"openssl {args[0]} -d"
        if name == "xxd" and any(re.fullmatch(r"-([a-z]?r[a-z]?|revert)", a) for a in args):
            return "xxd -r"
        if (shell or name in ("source", ".")) and any(a.startswith("<()") for a in args):
            return f"{name} <(...)"
        if shell:
            for j, a in enumerate(args[:-1]):
                if re.fullmatch(r"-[A-Za-z]*c", a) and args[j + 1].startswith("$"):
                    return f"{name} -c {args[j + 1][:20]}"
        if piped and (shell or is_interp):
            args = [a for a in args if not re.match(r"\d*[<>&]", a)]  # redirections
            prog = r"-[A-Za-z]*c[A-Za-z]*" if shell else r"-[A-Za-z]*[cemrE][A-Za-z]*|--(eval|command|print)"
            if not any(re.fullmatch(prog, a) or not a.startswith("-") and a != "-" for a in args):
                return f"| {name} (program read from stdin)"
    if interp:
        m = _DECODE_CALL.search(cmd)
        if m:
            return f"{m.group(0)} (in an interpreter program)"
    return None


# Backstop for script strings handed to commands the scanner doesn't follow
# (`watch 'eval "$x"'`): eval right after a quote with an expandable argument.
_QUOTE_EVAL = re.compile(r"['\"]\s*eval\s+[\"'$`]")

# Base64 literals long enough to hide a command; decoded and scanned so a
# destructive payload is named in the refusal, not just "opaque".
_B64_TOKEN = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")


def _decoded_texts(cmd: str):
    for tok in _B64_TOKEN.findall(cmd):
        try:
            text = base64.b64decode(tok + "=" * (-len(tok) % 4), validate=True).decode("utf-8")
        except ValueError:  # binascii.Error and UnicodeDecodeError both are
            continue
        if text and all(c.isprintable() or c.isspace() for c in text):
            yield text


def is_destructive(cmd: str, patterns: Optional[list] = None, _depth: int = 0) -> Optional[str]:
    """Return the matching destructive pattern (as a string) or None.

    `patterns` defaults to the host list (vm_ssh passes its own). They run on
    the raw text and on the quote-stripped words (`r''m -rf`). Base64
    payloads in the command are decoded and checked too, then the opaque
    wrappers: those match even when the payload looks harmless."""
    pats = patterns if patterns is not None else _DESTRUCTIVE_PATTERNS
    try:
        cmds = _commands(cmd)
    except _TooDeep:
        return "deeply nested substitutions (not inspectable)"
    words_text = "\n".join(" ".join(w) for _, w in cmds)
    for text in (cmd, words_text):
        for pat in pats:
            m = pat.search(text)
            if m:
                return m.group(0)
    if _depth < 3:
        for text in _decoded_texts(cmd):
            hit = is_destructive(text, patterns, _depth + 1)
            if hit:
                return f"{hit} (inside a base64 payload)"
    hit = _opaque(cmd, cmds)
    if hit is None:
        m = _QUOTE_EVAL.search(cmd)
        hit = m and m.group(0).strip()
    if hit:
        return f"{hit} (decoded/eval/piped-to-shell content is not inspectable)"
    return None


def audit_log(
    cmd: str,
    rc: Optional[int],
    *,
    note: str = "",
    stdout_preview: str = "",
    stderr_preview: str = "",
) -> None:
    """Append one hash-chained line per call, never raises."""
    audit.append(_AUDIT_PATH, {
        "ts": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "host": config.PROXMOX_SSH_HOST,
        "user": config.PROXMOX_SSH_USER,
        "rc": rc,
        "cmd": redact(cmd)[:500],  # redact before cutting a secret in half
        "note": note,
        "stdout_preview": redact(stdout_preview)[:200],
        "stderr_preview": redact(stderr_preview)[:200],
    })


async def _connect():
    if asyncssh is None:
        raise HostSshError(
            "asyncssh is not installed. Run: pip install -r requirements.txt"
        )
    if not config.PROXMOX_SSH_HOST:
        raise HostSshError("PROXMOX_SSH_HOST is not configured in .env")
    if not config.PROXMOX_SSH_KEY_PATH or not os.path.exists(config.PROXMOX_SSH_KEY_PATH):
        raise HostSshError(
            f"SSH key not found: {config.PROXMOX_SSH_KEY_PATH}"
        )

    known_hosts_arg = (
        None
        if (config.PROXMOX_SSH_KNOWN_HOSTS or "").lower() == "ignore"
        else config.PROXMOX_SSH_KNOWN_HOSTS
    )
    try:
        return await asyncssh.connect(
            host=config.PROXMOX_SSH_HOST,
            port=config.PROXMOX_SSH_PORT,
            username=config.PROXMOX_SSH_USER,
            client_keys=[config.PROXMOX_SSH_KEY_PATH],
            known_hosts=known_hosts_arg,
            connect_timeout=10,
        )
    except asyncssh.PermissionDenied as exc:
        raise HostSshError(f"SSH auth failed for Proxmox host: {exc}")
    except (asyncssh.HostKeyNotVerifiable, asyncssh.KeyExchangeFailed) as exc:
        raise HostSshError(f"Host key verification failed: {exc}")
    except OSError as exc:
        raise HostSshError(
            f"Cannot connect to {config.PROXMOX_SSH_HOST}:"
            f"{config.PROXMOX_SSH_PORT}: {exc}"
        )


async def exec_command(
    cmd: str,
    *,
    timeout: float = 60.0,
) -> tuple[int, str, str]:
    """Run a shell command on the Proxmox host. Returns (rc, stdout, stderr)."""
    conn = await _connect()
    try:
        try:
            result = await conn.run(cmd, check=False, timeout=timeout)
        except asyncssh.TimeoutError:
            raise HostSshError(f"Command timed out after {timeout}s on host")
        stdout = (result.stdout or "")[:MAX_OUTPUT_BYTES]
        stderr = (result.stderr or "")[:MAX_OUTPUT_BYTES]
        return result.exit_status or 0, stdout, stderr
    finally:
        conn.close()
        try:
            await conn.wait_closed()
        except Exception:
            pass


def format_host_ssh_error(exc: Exception) -> str:
    if isinstance(exc, HostSshError):
        return f"Error: {exc.message}"
    return f"Error: {type(exc).__name__}: {exc}"
