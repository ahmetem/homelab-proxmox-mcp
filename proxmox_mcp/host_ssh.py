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

import datetime as _dt
import json
import os
import re
from pathlib import Path
from typing import Optional

try:
    import asyncssh  # type: ignore
except ImportError:
    asyncssh = None  # type: ignore

from proxmox_mcp import config


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


def is_destructive(cmd: str) -> Optional[str]:
    """Return the matching destructive pattern (as a string) or None."""
    for pat in _DESTRUCTIVE_PATTERNS:
        m = pat.search(cmd)
        if m:
            return m.group(0)
    return None


def audit_log(
    cmd: str,
    rc: Optional[int],
    *,
    note: str = "",
    stdout_preview: str = "",
    stderr_preview: str = "",
) -> None:
    """Append one line per call, never raises."""
    try:
        ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = {
            "ts": ts,
            "host": config.PROXMOX_SSH_HOST,
            "user": config.PROXMOX_SSH_USER,
            "rc": rc,
            "cmd": cmd[:500],
            "note": note,
            "stdout_preview": stdout_preview[:200],
            "stderr_preview": stderr_preview[:200],
        }
        with _AUDIT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # never let logging break a real call


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
