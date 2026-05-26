"""SSH-backed disk preparation tools.

These exist to work around Proxmox endpoints that reject API tokens
(notably `wipedisk` and `initgpt`, which require root@pam interactive auth).

Each tool builds an argv list and hands it to proxmox_mcp.ssh.run_command.
No shell interpolation, no string concatenation of user input into commands.
"""
from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from proxmox_mcp import ssh
from proxmox_mcp.config import require_ssh
from proxmox_mcp.format import missing_confirm, missing_data_loss_ack
from proxmox_mcp.mcp_instance import mcp


_DEVICE_RE = re.compile(r"^/dev/[A-Za-z0-9/_-]+$")


def _validate_device(d: str) -> Optional[str]:
    if not _DEVICE_RE.fullmatch(d):
        return f"Invalid device path: {d!r}"
    if len(d) > 64:
        return f"Device path too long: {d!r}"
    return None


class SshDiskWipeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    disk: str = Field(
        ...,
        description="Block device path (e.g. '/dev/sdX', '/dev/nvme0n1').",
        min_length=1, max_length=64,
    )
    confirm: bool = Field(default=False)
    i_understand_data_loss: bool = Field(default=False)
    reason: Optional[str] = Field(default=None, max_length=200)

    @field_validator("disk")
    @classmethod
    def _check_disk(cls, v: str) -> str:
        err = _validate_device(v)
        if err:
            raise ValueError(err)
        return v


class SshDiskInitGptInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    disk: str = Field(..., min_length=1, max_length=64)
    confirm: bool = Field(default=False)
    reason: Optional[str] = Field(default=None, max_length=200)

    @field_validator("disk")
    @classmethod
    def _check_disk(cls, v: str) -> str:
        err = _validate_device(v)
        if err:
            raise ValueError(err)
        return v


@mcp.tool(
    name="proxmox_ssh_wipe_disk",
    annotations={
        "title": "Wipe Disk via SSH (DESTROY ALL DATA)",
        "readOnlyHint": False, "destructiveHint": True,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def proxmox_ssh_wipe_disk(params: SshDiskWipeInput) -> str:
    """Erase partition table and FS signatures on a disk via SSH.

    Use this when proxmox_wipe_disk fails with 'user != root@pam' (a known
    Proxmox limitation for API tokens). This runs `wipefs -a` directly on the
    node as the SSH user (typically root).

    Requires BOTH confirm=true AND i_understand_data_loss=true.
    All data on the disk is irretrievable after this completes.
    """
    cfg_err = require_ssh()
    if cfg_err:
        return cfg_err
    if not params.confirm:
        return missing_confirm("proxmox_ssh_wipe_disk")
    if not params.i_understand_data_loss:
        return missing_data_loss_ack("proxmox_ssh_wipe_disk")

    try:
        rc, out, err = await ssh.run_command(["wipefs", "-a", "-f", params.disk])
    except ssh.SshError as exc:
        return ssh.format_ssh_error(exc)

    if rc != 0:
        return (
            f"Error: wipefs failed (rc={rc}) on `{params.disk}`.\n"
            f"stderr: {err.strip() or '(empty)'}"
        )

    return (
        f"OK: `{params.disk}` wiped via SSH.\n"
        f"```\n{out.strip() or '(no output)'}\n```\n"
        "Verify with proxmox_list_disks."
    )


@mcp.tool(
    name="proxmox_ssh_init_gpt",
    annotations={
        "title": "Initialize GPT via SSH",
        "readOnlyHint": False, "destructiveHint": True,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def proxmox_ssh_init_gpt(params: SshDiskInitGptInput) -> str:
    """Write a fresh GPT partition table to a disk via SSH (sgdisk -Z -o).

    Use this when proxmox_disk_init_gpt fails due to token auth restrictions.
    `sgdisk -Z` zaps existing data; `-o` writes a new empty GPT.

    Requires confirm=true.
    """
    cfg_err = require_ssh()
    if cfg_err:
        return cfg_err
    if not params.confirm:
        return missing_confirm("proxmox_ssh_init_gpt")

    try:
        rc, out, err = await ssh.run_command(["sgdisk", "-Z", "-o", params.disk])
    except ssh.SshError as exc:
        return ssh.format_ssh_error(exc)

    if rc != 0:
        return (
            f"Error: sgdisk failed (rc={rc}) on `{params.disk}`.\n"
            f"stderr: {err.strip() or '(empty)'}"
        )
    return (
        f"OK: GPT initialized on `{params.disk}` via SSH.\n"
        f"```\n{out.strip() or '(no output)'}\n```"
    )
