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
# a pipe into a shell, a substituted or process-substituted script. Fail
# closed: they need the ack whatever the payload holds (`base64 -d | sh` of a
# harmless line too). Shared with vm_ssh.
# kisayol: shell wrappers only. Ceiling: quote-split words (`r''m -rf`),
# `| python3 -`, hex/printf escapes into a file run later, a payload built
# from variables still pass. Upgrade path: parse with a real shell lexer
# (bashlex) and check the normalised words, or an allow-list like host_read.
_OPAQUE_PATTERNS = [
    re.compile(r"\bbase(64|32)\b[^|;&\n]*\s-(-decode|[a-zA-Z]*[dD])\b"),
    re.compile(r"\bopenssl\s+(enc|base64)\b[^|;&\n]*\s-d\b"),
    re.compile(r"\bxxd\b[^|;&\n]*\s-[a-z]*r"),
    re.compile(r"\b(b64decode|uudecode)\b"),
    # eval as a command; after a quote (`sh -c 'eval "$x"'`) only with an
    # expandable argument, so `grep -E 'prompt eval|eval time'` stays clear.
    re.compile(r"(^|[;&(`{\n]|\b(then|do|else)\s)\s*eval\b"),
    re.compile(r"['\"]\s*eval\s+[\"'$`]"),
    re.compile(r"\|\s*(sudo\s+(-\S+\s+)*)?(\S*/)?(ba|da|z|k)?sh\b"),
    re.compile(r"\b(ba|da|z|k)?sh\s+-c\s+[\"']?(\$\(|`)"),
    re.compile(r"(\b(source|(ba|da|z|k)?sh)|(^|[;&|\s])\.)\s+<\("),
]

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

    `patterns` defaults to the host list (vm_ssh passes its own). Base64
    payloads in the command are decoded and checked too, then the opaque
    wrappers: those match even when the payload looks harmless."""
    for pat in patterns if patterns is not None else _DESTRUCTIVE_PATTERNS:
        m = pat.search(cmd)
        if m:
            return m.group(0)
    if _depth < 3:
        for text in _decoded_texts(cmd):
            hit = is_destructive(text, patterns, _depth + 1)
            if hit:
                return f"{hit} (inside a base64 payload)"
    for pat in _OPAQUE_PATTERNS:
        m = pat.search(cmd)
        if m:
            return (f"{m.group(0).strip()} (decoded/eval/piped-to-shell content "
                    "is not inspectable)")
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
