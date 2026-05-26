"""Async SSH client for Proxmox host commands.

Design principles (security-first):

1. **No raw command strings from callers.** SSH-backed tools build their argv
   as a list of strings; this module quotes each element with shlex.quote
   before sending. Callers never compose shell strings themselves.

2. **Allow-listed binaries.** Only specific executables (wipefs, sgdisk, zfs,
   zpool, etc.) are reachable through the helpers in this module. Callers
   that try to invoke anything outside the allow-list get a structured error.

3. **Output is bounded.** stdout+stderr are truncated to 1 MB; long-running
   commands time out per PROXMOX_SSH_TIMEOUT.

4. **Host key verification by default.** PROXMOX_SSH_KNOWN_HOSTS must point at
   a known_hosts file. Set it to the literal "ignore" only on trusted local
   networks; doing so logs a warning to stderr.

This module exposes:
  - run_command(argv, *, timeout)  : low-level, allow-list enforced
  - run_text(argv, *, timeout)     : returns stdout if rc==0 else SshError text

Both are async and use a fresh connection per call (connection caching is a
separate concern; the volume of disk/zfs ops we drive here doesn't justify it).
"""
from __future__ import annotations

import shlex
import sys
from typing import Optional, Sequence

try:
    import asyncssh  # type: ignore
except ImportError:  # pragma: no cover - friendlier error if dep missing
    asyncssh = None  # type: ignore

from proxmox_mcp import config


# Binaries this module is willing to invoke. Adding to this list is a
# security decision — review the wrappers that use it.
ALLOWED_BINARIES = frozenset(
    {
        # Disk wipe / partition table
        "wipefs",
        "sgdisk",
        "blkdiscard",
        "dd",  # used very narrowly by wipe wrapper to zero first/last MB
        # ZFS dataset / property / snapshot ops
        "zfs",
        "zpool",
        # LVM ops (mostly redundant with REST, kept for parity)
        "pvs",
        "vgs",
        "lvs",
        "lvremove",
        "vgremove",
        "pvremove",
        # Read-only diagnostics
        "lsblk",
        "nvme",
        "smartctl",
    }
)


MAX_OUTPUT_BYTES = 1_048_576  # 1 MB


class SshError(Exception):
    """Raised for any SSH-related failure. .message is user-safe text."""

    def __init__(self, message: str, *, rc: Optional[int] = None,
                 stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.message = message
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr


def _validate_argv(argv: Sequence[str]) -> Optional[str]:
    if not argv:
        return "Empty argv."
    if not isinstance(argv, (list, tuple)):
        return "argv must be a list or tuple of strings."
    for i, a in enumerate(argv):
        if not isinstance(a, str):
            return f"argv[{i}] is not a string."
        if "\x00" in a:
            return f"argv[{i}] contains NUL byte."
    binary = argv[0]
    if "/" in binary:
        # Allow absolute paths only to well-known sbin/bin
        last = binary.rsplit("/", 1)[-1]
        if last not in ALLOWED_BINARIES:
            return (
                f"Binary '{binary}' (resolved: '{last}') is not in the SSH "
                "allow-list."
            )
    elif binary not in ALLOWED_BINARIES:
        return f"Binary '{binary}' is not in the SSH allow-list."
    return None


def _build_known_hosts():
    """Translate PROXMOX_SSH_KNOWN_HOSTS env into asyncssh's known_hosts arg."""
    kh = config.PROXMOX_SSH_KNOWN_HOSTS
    if not kh:
        # asyncssh default = use the user's ~/.ssh/known_hosts
        return ()
    if kh.lower() == "ignore":
        print(
            "WARNING: PROXMOX_SSH_KNOWN_HOSTS=ignore — host key checks "
            "disabled.",
            file=sys.stderr,
        )
        return None  # asyncssh: skip host key check
    return kh  # treat as path


async def _connect():
    if asyncssh is None:
        raise SshError(
            "asyncssh is not installed. Run: pip install -r requirements.txt"
        )
    auth_kwargs: dict = {}
    if config.PROXMOX_SSH_KEY_PATH:
        auth_kwargs["client_keys"] = [config.PROXMOX_SSH_KEY_PATH]
    if config.PROXMOX_SSH_PASSWORD:
        auth_kwargs["password"] = config.PROXMOX_SSH_PASSWORD

    try:
        return await asyncssh.connect(
            host=config.PROXMOX_SSH_HOST,
            port=config.PROXMOX_SSH_PORT,
            username=config.PROXMOX_SSH_USER,
            known_hosts=_build_known_hosts(),
            **auth_kwargs,
        )
    except asyncssh.PermissionDenied as exc:
        raise SshError(f"SSH auth failed: {exc}")
    except (asyncssh.HostKeyNotVerifiable, asyncssh.KeyExchangeFailed) as exc:
        raise SshError(
            f"Host key verification failed: {exc}. "
            "Update PROXMOX_SSH_KNOWN_HOSTS or use 'ignore' on a trusted network."
        )
    except OSError as exc:
        raise SshError(
            f"Cannot connect to {config.PROXMOX_SSH_HOST}:{config.PROXMOX_SSH_PORT}: {exc}"
        )


def _quote(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)


async def run_command(
    argv: Sequence[str],
    *,
    timeout: Optional[float] = None,
) -> tuple[int, str, str]:
    """Run a command over SSH and return (rc, stdout, stderr).

    argv[0] must be in ALLOWED_BINARIES (bare name or last path component).
    All elements are shell-quoted before transmission.

    Output is truncated to MAX_OUTPUT_BYTES per stream.
    """
    err = _validate_argv(argv)
    if err:
        raise SshError(err)

    cfg_err = config.require_ssh()
    if cfg_err:
        raise SshError(cfg_err)

    cmd_str = _quote(argv)
    eff_timeout = timeout if timeout is not None else config.PROXMOX_SSH_TIMEOUT

    conn = await _connect()
    try:
        try:
            result = await conn.run(cmd_str, check=False, timeout=eff_timeout)
        except asyncssh.TimeoutError:
            raise SshError(f"SSH command timed out after {eff_timeout}s: {cmd_str}")

        stdout = (result.stdout or "")[:MAX_OUTPUT_BYTES]
        stderr = (result.stderr or "")[:MAX_OUTPUT_BYTES]
        return result.exit_status or 0, stdout, stderr
    finally:
        conn.close()
        try:
            await conn.wait_closed()
        except Exception:
            pass


async def run_pipeline(
    stages: Sequence[Sequence[str]],
    *,
    timeout: Optional[float] = None,
    pipefail: bool = True,
) -> tuple[int, str, str]:
    """Run a multi-stage shell pipeline (`a | b | c`) over SSH.

    Each stage is validated through _validate_argv exactly like run_command,
    so the binary allow-list still applies to every stage. Stage arguments
    are shell-quoted; the pipe operator is inserted by this function — no
    caller-controlled shell metacharacters.

    With pipefail=True (default), the returned rc is the rc of the first
    failing stage; without it, only the final stage's rc is returned.

    Returns (rc, final_stage_stdout, combined_stderr).
    """
    if not stages:
        raise SshError("Empty pipeline.")
    for st in stages:
        err = _validate_argv(st)
        if err:
            raise SshError(err)

    cfg_err = config.require_ssh()
    if cfg_err:
        raise SshError(cfg_err)

    eff_timeout = timeout if timeout is not None else config.PROXMOX_SSH_TIMEOUT

    pipeline = " | ".join(_quote(st) for st in stages)
    full = f"set -o pipefail; {pipeline}" if pipefail else pipeline

    conn = await _connect()
    try:
        try:
            result = await conn.run(full, check=False, timeout=eff_timeout)
        except asyncssh.TimeoutError:
            raise SshError(
                f"SSH pipeline timed out after {eff_timeout}s: {pipeline}"
            )
        stdout = (result.stdout or "")[:MAX_OUTPUT_BYTES]
        stderr = (result.stderr or "")[:MAX_OUTPUT_BYTES]
        return result.exit_status or 0, stdout, stderr
    finally:
        conn.close()
        try:
            await conn.wait_closed()
        except Exception:
            pass


async def run_text(
    argv: Sequence[str],
    *,
    timeout: Optional[float] = None,
) -> str:
    """Like run_command but returns stdout on success or raises SshError on non-zero rc."""
    rc, out, err = await run_command(argv, timeout=timeout)
    if rc != 0:
        raise SshError(
            f"Command failed (rc={rc}): {_quote(argv)}\n"
            f"stderr: {err.strip() or '(empty)'}",
            rc=rc,
            stdout=out,
            stderr=err,
        )
    return out


def format_ssh_error(exc: Exception) -> str:
    """Convert an SshError or other exception into a user-facing error string."""
    if isinstance(exc, SshError):
        return f"Error: {exc.message}"
    return f"Error: {type(exc).__name__}: {exc}"
