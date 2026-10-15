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
# `$'…'` is decoded (`$'\x72m'` is `rm`), `$"…"` is a double-quoted string,
# backticks honour `\``, `$((…))` is arithmetic (its `$(…)` followed),
# heredoc bodies end at their delimiter and are scanned as scripts (an
# unquoted delimiter's `$(…)` too).
# Fail-closed (`_Uninspectable`): what the scanner can't model needs the ack.
# An unclosed quote, `$'`, backtick, `$(`, `$((` or heredoc (a misparse
# usually leaves one open), an unknown `$'` escape, a wrapper option missing
# from `_WRAPPERS` (`sudo -h host`, `env -S` too), brace expansion in a head
# word. Not in a decoded base64 payload or a heredoc body no shell reads
# (`psql <<SQL`, `cat > f <<EOF`): those are data (SQL, a diff) as often as
# scripts, so there only the patterns and opaque wrappers count. Replay
# (madde 310): of 9 505 full-length exec commands from the transcripts,
# those newly refused were all malformed for bash too.
# kisayol: a scanner, not a shell. Ceiling: payloads built from variables or
# printf escapes, a script string handed to anything but a shell/su/runuser
# `-c`, ssh or pct/qm/lxc-attach/runuser `--` unless it starts with `eval`
# (quote-eval backstop: `trap`, `parallel`…), `| su user` (its operand is a
# user), interpreter flags read as "program given" (`| python3 -E`),
# `$( (…) )` and `case … )` closing `$(` early, a script written to a file
# by a heredoc and run later (scanned leniently), `(( a << b ))` read as a
# heredoc (fails closed). Upgrade path: a real shell parser (bashlex, a new
# dependency, madde 309) or an allow-list like host_read.
_SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "ash", "mksh"}
# hand their `-c` string to a shell; `runuser -u x -- cmd` runs cmd as argv
_SCRIPT_RUNNERS = _SHELLS | {"su", "runuser"}
_INTERP = re.compile(r"(python|pypy)[0-9.]*|perl|ruby|node(js)?|php[0-9.]*")
# wrapper -> (short options taking a value, short flags, long options taking a
# value, long flags, positional args before the command). From `--help` of
# coreutils 9 / sudo 1.9 / util-linux setsid; doas(1), GNU time(1), bash
# builtins. Anything else is refused as unmodelled, not guessed: `sudo -h` is
# help alone but takes a host before a command. xargs `-e/-i/-l` and the
# `[=X]` long options take a value only attached.
_WRAPPERS = {
    "sudo": ("CDgpRrtTUua", "AbBEeHiKklnPSsVv",
             "close-from chdir group prompt chroot role type command-timeout "
             "other-user user host auth-type",
             "askpass background bell preserve-env edit set-home help login "
             "remove-timestamp reset-timestamp list non-interactive "
             "preserve-groups stdin shell version validate", 0),
    "doas": ("aCu", "Lns", "", "", 0),
    "env": ("auCS", "i0v", "argv0 unset chdir split-string",
            "ignore-environment null block-signal default-signal ignore-signal "
            "list-signal-handling debug help version", 0),
    "xargs": ("adEILnPs", "0eiloprtx",
              "arg-file delimiter max-lines max-args max-procs max-chars "
              "process-slot-var",
              "null eof replace open-tty interactive no-run-if-empty "
              "show-limits verbose exit help version", 0),
    "nice": ("n", "", "adjustment", "help version", 0),
    "timeout": ("ks", "fpv", "kill-after signal",
                "foreground preserve-status verbose help version", 1),
    "stdbuf": ("ioe", "", "input output error", "help version", 0),
    "chroot": ("", "", "groups userspec", "skip-chdir help version", 1),
    "nohup": ("", "", "", "help version", 0),
    "exec": ("a", "cl", "", "", 0),
    "command": ("", "pvV", "", "", 0),
    "builtin": ("", "", "", "", 0),
    "time": ("fo", "apqvV", "format output",
             "append portability quiet verbose help version", 0),
    "setsid": ("", "cfwhV", "", "ctty fork wait help version", 0),
}
_KEYWORDS = {"if", "then", "do", "else", "elif", "while", "until", "!", "{", "}"}
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_REDIR = re.compile(r"\d*[<>&](?!\(\))")  # not a `<(…)` word
_REDIR_OP = re.compile(r"\d*(<<<|&>>?|>>|>\||<>|[<>]&?)")  # alone: its target is the next word
# Decode calls inside an interpreter's program (python -c, perl -e, heredoc).
# Only when an interpreter is a command head, so `grep -rn b64decode` passes.
_DECODE_CALL = re.compile(r"\b(b64decode|decodebytes|a2b_base64|decode_base64|decode64|uudecode)\b")
_MAX_NEST = 20
# bash's ANSI-C quoting escapes; any other `\X` inside `$'…'` is refused
_ANSI_C = re.compile(r"\\(x[0-9A-Fa-f]{1,2}|u[0-9A-Fa-f]{1,4}|U[0-9A-Fa-f]{1,8}"
                     r"|[0-7]{1,3}|c.|[abeEfnrtv\\'\"?])", re.DOTALL)
_ANSI_C_CHARS = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f",
                 "n": "\n", "r": "\r", "t": "\t", "v": "\v"}


class _Uninspectable(Exception):
    """A construct the scanner can't model: the gate fails closed."""


def _ansi_c(body: str) -> str:
    """The text of a `$'…'` body, as bash decodes it."""
    out, i = [], 0
    while i < len(body):
        if body[i] != "\\":
            out.append(body[i])
            i += 1
            continue
        m = _ANSI_C.match(body, i)
        if not m:
            raise _Uninspectable(f"$'…' escape {body[i:i + 2]!r}")
        e, i = m.group(1), m.end()
        if e[0] in "xuU":
            out.append(chr(min(int(e[1:], 16), 0x10FFFF)))
        elif e[0] in "01234567":
            out.append(chr(int(e, 8) & 0xFF))
        elif e[0] == "c":
            out.append(chr(ord(e[1]) & 0x1F))
        else:
            out.append(_ANSI_C_CHARS.get(e, e))
    return "".join(out)


def _scan(s: str, out: list, depth: int = 0, i: int = 0, close: str = "",
          expand: bool = False, lenient: bool = False) -> int:
    """Append (piped, words, lenient) for each simple command in `s` to `out`.

    Words have quotes and escapes removed; a `$(…)`, backtick or `<(…)` leaves
    the marker `$()`/`<()` in its word and its own commands in `out`. Returns
    the index after `close` (or len(s)). `expand`: only the substitutions in
    `s` (an unquoted heredoc body, `$((…))`). `lenient`: `s` is a heredoc body,
    maybe data: unclosed constructs are not refused, nor `_head` failures."""
    if depth > _MAX_NEST:
        raise _Uninspectable("deeply nested substitutions")
    words: list = []
    word: list = []
    has_word = False
    piped = False
    heredocs: list = []  # (delimiter, strip tabs, quoted, its command's words) awaiting a newline
    n = len(s)

    def unclosed(what: str) -> None:
        if not lenient:
            raise _Uninspectable(f"unclosed {what}")

    def end_word():
        nonlocal word, has_word
        if has_word:
            words.append("".join(word))
        word, has_word = [], False

    def end_cmd(next_piped: bool):
        nonlocal words, piped
        end_word()
        if words:
            out.append((piped, words, lenient))
            words, piped = [], next_piped
        else:  # `a | (sh)`, `a |<newline> sh`: the pipe still feeds the next command
            piped = piped or next_piped

    def subst(j: int, marker: str) -> int:  # j: index after the opening `(`
        nonlocal has_word
        word.append(marker)
        has_word = True
        return _scan(s, out, depth + 1, j, ")", lenient=lenient)

    def backtick(j: int) -> int:  # j: index after the opening backtick
        nonlocal has_word
        inner, k = [], j
        while k < n and s[k] != "`":
            if s[k] == "\\" and s[k + 1:k + 2] in ("`", "\\", "$"):
                k += 1  # bash drops the backslash before these three only
            inner.append(s[k:k + 1])
            k += 1
        if k >= n:
            unclosed("`")
        _scan("".join(inner), out, depth + 1, lenient=lenient)
        word.append("$()")
        has_word = True
        return k + 1

    def arith(j: int) -> Optional[int]:  # j at `$((`; None: it is `$( (…) …)`
        k, level = j + 3, 2
        while k < n:
            if s[k] == "(":
                level += 1
            elif s[k] == ")":
                level -= 1
                if level == 1:
                    if s[k + 1:k + 2] != ")":
                        return None
                    # `$(( $(stat -c %s f) / 1024 ))`; `<<` here is a shift
                    _scan(s[j + 3:k], out, depth + 1, expand=True, lenient=lenient)
                    return k + 2
            k += 1
        unclosed("$((")
        return n

    def dquote(j: int, term: str) -> int:  # double-quoted text up to `term`
        nonlocal has_word
        has_word = True
        while j < n and s[j] != term:
            if s[j] == "\\" and s[j + 1:j + 2] in ('"', "\\", "$", "`", "\n"):
                word.append(s[j + 1])
                j += 2
            elif s.startswith("$((", j) and (k := arith(j)) is not None:
                word.append("$(())")
                j = k
            elif s.startswith("$(", j):
                j = subst(j + 2, "$()")
            elif s[j] == "`":
                j = backtick(j + 1)
            else:
                word.append(s[j])
                j += 1
        if j >= n and term:
            unclosed('"')
        return j + 1

    def heredoc(j: int) -> int:  # j: index after `<<`; registers the delimiter
        nonlocal has_word
        strip = s.startswith("-", j)
        j += strip
        while j < n and s[j] in " \t":
            j += 1
        delim, quoted = [], False
        while j < n and s[j] not in " \t\r\n;&|()<>":
            if s[j] in "'\"":
                k = s.find(s[j], j + 1)
                k = n if k < 0 else k
                delim.append(s[j + 1:k])
                quoted, j = True, k + 1
            elif s[j] == "\\":
                delim.append(s[j + 1:j + 2])
                quoted, j = True, j + 2
            else:
                delim.append(s[j])
                j += 1
        if delim:
            heredocs.append(("".join(delim), strip, quoted, words))
        else:
            unclosed("heredoc (no delimiter)")
        word.append("<<")
        has_word = True
        return j

    def bodies(j: int) -> int:  # j: start of the line after the command line
        for delim, strip, quoted, cmd_words in heredocs:
            lines = []
            while True:
                if j >= n:
                    unclosed(f"heredoc <<{delim[:20]}")
                    break
                e = s.find("\n", j)
                e = n if e < 0 else e
                line, j = s[j:e], e + 1
                if (line.lstrip("\t") if strip else line) == delim:
                    break
                lines.append(line)
            text = "\n".join(lines)
            # strict when a shell reads it as its program (`bash <<EOF`,
            # `pct exec N -- sh <<EOF`); else it may be data (`psql <<SQL`,
            # `cat > f <<EOF`, `sh -c 'python3 -' <<PY`)
            at = next((k for k, w in enumerate(cmd_words) if w.rsplit("/", 1)[-1] in _SHELLS), None)
            data = lenient or at is None or not _program_from_stdin(True, cmd_words[at + 1:])
            _scan(text, out, depth + 1, lenient=data)
            if not quoted:  # bash runs the body's `$(…)`/backticks: strict even in data
                _scan(text, out, depth + 1, expand=True, lenient=lenient)
        heredocs.clear()
        return j

    if expand:
        dquote(i, "")
        return n
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
            if k < 0:
                unclosed("'")
                k = n
            word.append(s[i + 1:k])
            has_word = True
            i = k + 1
        elif c == '"':
            i = dquote(i + 1, '"')
        elif s.startswith("$'", i):  # ANSI-C quoting: `\'` does not close it
            k = i + 2
            while k < n and s[k] != "'":
                k += 2 if s[k] == "\\" else 1
            if k >= n:
                unclosed("$'")
                word.append(s[i + 2:])
            else:
                word.append(_ansi_c(s[i + 2:k]))
            has_word = True
            i = k + 1
        elif s.startswith('$"', i):  # locale quoting: a double-quoted string
            i += 1
        elif s.startswith("$((", i) and (k := arith(i)) is not None:
            word.append("$(())")
            has_word = True
            i = k
        elif c == "$" and s.startswith("$(", i):
            i = subst(i + 2, "$()")
        elif s.startswith("<<<", i):  # here-string: one word, not a heredoc
            word.append("<<<")
            has_word = True
            i += 3
        elif s.startswith("<<", i):
            i = heredoc(i + 2)
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
            if c == "\n" and heredocs:
                i = bodies(i)
        elif c in " \t\r":
            end_word()
            i += 1
        else:
            word.append(c)
            has_word = True
            i += 1
    end_cmd(False)
    if close:
        unclosed("$(")
    if heredocs:
        unclosed("heredoc (no body)")
    return n


def _skip_options(name: str, words: list, k: int) -> Optional[int]:
    """Index of the command after wrapper `name`'s options and positional args
    (words[k] is its first argument). None for `command -v` (a lookup).
    An option `_WRAPPERS` doesn't list is refused, not guessed."""
    sval, sflag, lval, lflag, positional = _WRAPPERS[name]
    lval, lflag = lval.split(), lflag.split()
    while k < len(words) and words[k].startswith("-"):
        opt = words[k]
        k += 1
        if opt == "--":
            break
        if opt == "-" and name == "env":  # `env -` is `env -i`
            continue
        if name == "nice" and re.fullmatch(r"--?\d+", opt):  # `nice -5`
            continue
        if opt.startswith("--"):
            key, eq, _ = opt[2:].partition("=")
            hits = [o for o in lval + lflag if o.startswith(key)]  # getopt takes unique prefixes
            full = key if key in hits else hits[0] if len(hits) == 1 else None
            if full is None or full == "split-string":
                raise _Uninspectable(f"{name} {opt[:40]}")
            if full in lval and not eq:
                k += 1
            continue
        if opt == "-":
            raise _Uninspectable(f"{name} -")
        if name == "sudo" and opt == "-h" and all(re.match(r"\d*[<>&]", w) for w in words[k:]):
            return len(words)  # `sudo -h 2>&1`: help, no command
        for p, ch in enumerate(opt[1:], 2):
            if ch not in sval + sflag or name == "env" and ch == "S":
                raise _Uninspectable(f"{name} {opt[:40]}")
            if name == "command" and ch in "vV":
                return None  # `command -v eval` looks a name up
            if ch in sval:
                k += p == len(opt)  # the value is the next word unless attached
                break
    return k + positional


def _drop_redirections(words: list) -> list:
    """`words` without redirections (`2>&1`, `>f`, `> f`, `<<` of a heredoc)."""
    out, k = [], 0
    while k < len(words):
        if _REDIR.match(words[k]):
            k += 2 if _REDIR_OP.fullmatch(words[k]) else 1
        else:
            out.append(words[k])
            k += 1
    return out


def _head(words: list) -> tuple:
    """(command basename, its args) after keywords, assignments and wrappers.

    The single definition of "command position" every opaque check uses.
    Redirections go first: `2>/dev/null eval x` runs eval."""
    words = _drop_redirections(words)
    k: Optional[int] = 0
    while k < len(words):
        w = words[k]
        if w in _KEYWORDS or _ASSIGN.match(w):
            k += 1
            continue
        name = w.rsplit("/", 1)[-1]
        if name not in _WRAPPERS:
            break
        k = _skip_options(name, words, k + 1)
        if k is None:
            return "", []
    if k >= len(words):
        return "", []
    head = words[k]
    if "{" in head and ("," in head or ".." in head):  # `{eval,x}` expands to `eval x`
        raise _Uninspectable(f"brace expansion {head[:40]}")
    return head.rsplit("/", 1)[-1], words[k + 1:]


def _head_of(words: list, lenient: bool) -> tuple:
    """`_head`, except that a heredoc-body line it can't model is skipped:
    the body may be data (`psql <<SQL` with `'{"a":1,"b":2}'`)."""
    try:
        return _head(words)
    except _Uninspectable:
        if lenient:
            return "", []
        raise


def _scripts(words: list, lenient: bool = False):
    """Strings in one simple command that another shell will parse."""
    name, args = _head_of(words, lenient)
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


def _commands(cmd: str, lenient: bool = False) -> list:
    """Every simple command in `cmd`, nested scripts included."""
    out: list = []
    _scan(cmd, out, lenient=lenient)
    j = 0
    while j < len(out):  # each script is shorter than its parent: terminates
        _, words, lenient = out[j]
        for script in _scripts(words, lenient):
            _scan(script, out, lenient=lenient)
        j += 1
    return out


def _program_from_stdin(shell: bool, args: list) -> bool:
    """A shell (or interpreter) given these args reads its program from stdin."""
    args = _drop_redirections(args)  # `| bash > /tmp/log` gives bash no program
    prog = r"-[A-Za-z]*c[A-Za-z]*" if shell else r"-[A-Za-z]*[cemrE][A-Za-z]*|--(eval|command|print)"
    return not any(re.fullmatch(prog, a) or not a.startswith("-") and a != "-" for a in args)


def _opaque(cmd: str, cmds: list) -> Optional[str]:
    """Name the first opaque construct among the simple commands, or None."""
    interp = False
    for piped, words, lenient in cmds:
        name, args = _head_of(words, lenient)
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
        if piped and (shell or is_interp) and _program_from_stdin(shell, args):
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
    wrappers: those match even when the payload looks harmless. A construct
    the scanner can't model (`_Uninspectable`) needs the flag too, except in
    a decoded payload (`_depth` > 0): that is data (a diff, SQL) more often
    than a script, so only its patterns and opaque wrappers count."""
    pats = patterns if patterns is not None else _DESTRUCTIVE_PATTERNS
    unmodelled: Optional[str] = None
    try:
        cmds = _commands(cmd, lenient=_depth > 0)
        opaque = _opaque(cmd, cmds)
    except _Uninspectable as exc:
        cmds, opaque = [], None
        if not _depth:
            unmodelled = f"{exc} (the gate can't parse it: not inspectable)"
    words_text = "\n".join(" ".join(w) for _, w, _ in cmds)
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
    if unmodelled:
        return unmodelled
    hit = opaque
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
