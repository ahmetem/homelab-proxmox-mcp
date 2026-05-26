"""Async SSH client for guest VMs (not the Proxmox host itself).

Differences from proxmox_mcp/ssh.py (the Proxmox-host client):

1. **Multi-host registry**. Hosts loaded from vm_ssh_hosts.json next to .env.
   Tools accept an `alias` and resolve host/user/key from the registry.

2. **No binary allow-list**. The user explicitly opted into full shell exec
   (see chat log). Safety is reduced to:
     - confirm=true on every exec
     - regex check for obviously destructive patterns -> requires
       i_understand_data_loss=true
     - every call appended to _vm_ssh_audit.log

3. **Per-call shell access**. Tools build a single command string and run
   it via the user's login shell (typically bash). Multi-line commands and
   pipes work.

This module exposes:
  - load_registry()              -> dict[alias, HostSpec]
  - resolve(alias)               -> HostSpec
  - exec_command(alias, cmd, *, timeout) -> (rc, stdout, stderr)
  - audit_log(...)               -> appends a structured line
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import asyncssh  # type: ignore
except ImportError:
    asyncssh = None  # type: ignore

from proxmox_mcp import config


# Same cap as proxmox_mcp/ssh.py
MAX_OUTPUT_BYTES = 1_048_576

# Default registry location: next to .env (repo root)
_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "vm_ssh_hosts.json"

# Audit log: same directory as registry
_AUDIT_PATH = Path(__file__).resolve().parent.parent / "_vm_ssh_audit.log"


# Patterns that look destructive enough to require explicit ack.
# Not exhaustive — this is a guardrail against typos, not a security control.
# (The user has opted into full shell exec; allow-list is intentionally absent.)
_DESTRUCTIVE_PATTERNS = [
    re.compile(r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r", re.IGNORECASE),
    re.compile(r"\brm\s+-rf?\s+/", re.IGNORECASE),
    re.compile(r"\bmkfs\b", re.IGNORECASE),
    re.compile(r"\bdd\s+[^|]*\bof=/dev/", re.IGNORECASE),
    re.compile(r">\s*/dev/(sd|nvme|vd|hd)", re.IGNORECASE),
    re.compile(r"\bshred\b", re.IGNORECASE),
    re.compile(r"\bwipefs\b", re.IGNORECASE),
    re.compile(r"\bparted\b.*\b(mklabel|rm)\b", re.IGNORECASE),
    re.compile(r"\bdocker\s+system\s+prune\b", re.IGNORECASE),
    re.compile(r"\bdocker\s+volume\s+rm\b", re.IGNORECASE),
    re.compile(r":\(\)\s*\{.*\}\s*;\s*:", re.DOTALL),  # fork bomb
    re.compile(r"\bshutdown\b|\bhalt\b|\bpoweroff\b|\breboot\b", re.IGNORECASE),
    re.compile(r"\bchmod\s+(-R\s+)?[0-7]*[0-7][0-7][0-7]\s+/", re.IGNORECASE),
    re.compile(r"\bchown\s+-R\b.*\s+/\b", re.IGNORECASE),
]


@dataclass(frozen=True)
class HostSpec:
    alias: str
    host: str
    port: int
    user: str
    key_path: str
    known_hosts: Optional[str]  # path, "ignore" -> None for asyncssh
    description: str


class VmSshError(Exception):
    """User-safe SSH error."""

    def __init__(self, message: str, *, rc: Optional[int] = None,
                 stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.message = message
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr


def load_registry() -> dict[str, HostSpec]:
    """Load and validate vm_ssh_hosts.json. Returns {alias: HostSpec}."""
    if not _REGISTRY_PATH.exists():
        return {}
    try:
        raw = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise VmSshError(f"vm_ssh_hosts.json is invalid JSON: {exc}")

    out: dict[str, HostSpec] = {}
    for alias, spec in raw.items():
        if alias.startswith("_"):  # comment keys
            continue
        if not isinstance(spec, dict):
            continue
        host = str(spec.get("host", "")).strip()
        if not host:
            raise VmSshError(f"Alias '{alias}' has no host.")
        user = str(spec.get("user", "")).strip()
        if not user:
            raise VmSshError(f"Alias '{alias}' has no user.")
        port = int(spec.get("port", 22))
        key_path = str(spec.get("key_path", "") or config.PROXMOX_SSH_KEY_PATH)
        if not key_path:
            raise VmSshError(
                f"Alias '{alias}' has no key_path and PROXMOX_SSH_KEY_PATH is empty."
            )
        known_hosts = spec.get("known_hosts", "ignore")
        out[alias] = HostSpec(
            alias=alias,
            host=host,
            port=port,
            user=user,
            key_path=key_path,
            known_hosts=str(known_hosts) if known_hosts else "ignore",
            description=str(spec.get("description", "")),
        )
    return out


def resolve(alias: str) -> HostSpec:
    reg = load_registry()
    if alias not in reg:
        known = ", ".join(sorted(reg.keys())) or "(empty)"
        raise VmSshError(
            f"Alias '{alias}' not in vm_ssh_hosts.json. Known: {known}"
        )
    return reg[alias]


def is_destructive(cmd: str) -> Optional[str]:
    """Return the matching destructive pattern (as a string) or None."""
    for pat in _DESTRUCTIVE_PATTERNS:
        m = pat.search(cmd)
        if m:
            return m.group(0)
    return None


def audit_log(
    alias: str,
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
            "alias": alias,
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


async def _connect(spec: HostSpec):
    if asyncssh is None:
        raise VmSshError(
            "asyncssh is not installed. Run: pip install -r requirements.txt"
        )
    if not os.path.exists(spec.key_path):
        raise VmSshError(
            f"Key file not found: {spec.key_path}"
        )

    known_hosts_arg = None if (spec.known_hosts or "").lower() == "ignore" else spec.known_hosts
    try:
        return await asyncssh.connect(
            host=spec.host,
            port=spec.port,
            username=spec.user,
            client_keys=[spec.key_path],
            known_hosts=known_hosts_arg,
            connect_timeout=10,
        )
    except asyncssh.PermissionDenied as exc:
        raise VmSshError(f"SSH auth failed for {spec.alias}: {exc}")
    except (asyncssh.HostKeyNotVerifiable, asyncssh.KeyExchangeFailed) as exc:
        raise VmSshError(
            f"Host key verification failed for {spec.alias}: {exc}"
        )
    except OSError as exc:
        raise VmSshError(
            f"Cannot connect to {spec.host}:{spec.port}: {exc}"
        )


async def exec_command(
    alias: str,
    cmd: str,
    *,
    timeout: float = 60.0,
) -> tuple[int, str, str]:
    """Run a shell command on the named VM. Returns (rc, stdout, stderr).

    Output is capped at MAX_OUTPUT_BYTES per stream.
    """
    spec = resolve(alias)
    conn = await _connect(spec)
    try:
        try:
            result = await conn.run(cmd, check=False, timeout=timeout)
        except asyncssh.TimeoutError:
            raise VmSshError(f"Command timed out after {timeout}s on {alias}")
        stdout = (result.stdout or "")[:MAX_OUTPUT_BYTES]
        stderr = (result.stderr or "")[:MAX_OUTPUT_BYTES]
        return result.exit_status or 0, stdout, stderr
    finally:
        conn.close()
        try:
            await conn.wait_closed()
        except Exception:
            pass


def format_vm_ssh_error(exc: Exception) -> str:
    if isinstance(exc, VmSshError):
        return f"Error: {exc.message}"
    return f"Error: {type(exc).__name__}: {exc}"
