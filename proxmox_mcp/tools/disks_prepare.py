"""Phase 2: disk preparation (wipe + GPT init) — API with SSH fallback.

One tool covers what used to be four (proxmox_disk_init_gpt, proxmox_wipe_disk,
proxmox_ssh_init_gpt, proxmox_ssh_wipe_disk). Proxmox rejects the REST
`wipedisk`/`initgpt` endpoints for API tokens ("user != root@pam"); with
via='auto' (default) the tool tries the REST endpoint first and transparently
falls back to running `wipefs -a -f` / `sgdisk -Z -o` over SSH.

Gates: wipe requires confirm=true AND i_understand_data_loss=true;
init_gpt requires confirm=true.
"""
from __future__ import annotations

from typing import Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field

from proxmox_mcp import config, http_client, ssh
from proxmox_mcp.format import missing_confirm, missing_data_loss_ack
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import WAIT_DESC


class DiskPrepareInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(
        ...,
        description=(
            "'wipe' erases partition table + FS signatures (ALL DATA LOST; "
            "confirm + i_understand_data_loss). 'init_gpt' writes a fresh GPT "
            "(confirm; refused by the API if the disk carries data — wipe first)."
        ),
        pattern="^(wipe|init_gpt)$",
    )
    node: str = Field(..., description="Node name", min_length=1)
    disk: str = Field(
        ...,
        description="Block device path (e.g. '/dev/sdX', '/dev/nvme0n1').",
        min_length=1,
        max_length=64,
        pattern=r"^/dev/[A-Za-z0-9/_-]+$",
    )
    via: str = Field(
        default="auto",
        description=(
            "'auto' (API, then SSH on token-permission failure), 'api', or "
            "'ssh' (direct wipefs/sgdisk on the host)."
        ),
        pattern="^(auto|api|ssh)$",
    )
    uuid: Optional[str] = Field(
        default=None,
        description="init_gpt via API only: optional disk UUID safety check.",
        max_length=64,
    )
    confirm: bool = Field(
        default=False,
        description="Must be true to execute. Only set after explicit user confirmation.",
    )
    i_understand_data_loss: bool = Field(
        default=False,
        description="Required for action='wipe' — all data on the disk is irretrievable.",
    )
    wait_seconds: int = Field(default=0, ge=0, le=600, description=WAIT_DESC)
    reason: Optional[str] = Field(
        default=None, description="Optional note about why", max_length=200
    )


def _is_token_auth_error(exc: Exception) -> bool:
    """True when the REST endpoint rejected the API token (the known
    'user != root@pam' limitation of wipedisk/initgpt)."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    body = exc.response.text or ""
    return "root@pam" in body or exc.response.status_code == 403


async def _via_api(params: DiskPrepareInput) -> tuple[Optional[str], Optional[Exception]]:
    """Run the REST call; return (message, None) or (None, exception)."""
    if params.action == "wipe":
        payload: dict = {"disk": params.disk}
        endpoint = f"/nodes/{params.node}/disks/wipedisk"
    else:
        payload = {"disk": params.disk}
        if params.uuid:
            payload["uuid"] = params.uuid
        endpoint = f"/nodes/{params.node}/disks/initgpt"
    try:
        task_id = await http_client.put(endpoint, data=payload)
    except Exception as exc:
        return None, exc
    verb = "Wipe" if params.action == "wipe" else "GPT init"
    suffix = await http_client.wait_for_task(params.node, task_id, params.wait_seconds)
    if not suffix:
        suffix = " Verify with proxmox_list_disks once the task completes."
    return (
        f"OK: {verb} started on `{params.disk}` ({params.node}). "
        f"Task: {task_id}.{suffix}"
    ), None


async def _via_ssh(params: DiskPrepareInput, fallback_note: str = "") -> str:
    cfg_err = config.require_ssh()
    if cfg_err:
        return cfg_err
    argv = (
        ["wipefs", "-a", "-f", params.disk]
        if params.action == "wipe"
        else ["sgdisk", "-Z", "-o", params.disk]
    )
    try:
        rc, out, err = await ssh.run_command(argv)
    except ssh.SshError as exc:
        return ssh.format_ssh_error(exc)
    if rc != 0:
        return (
            f"Error: {argv[0]} failed (rc={rc}) on `{params.disk}`.\n"
            f"stderr: {err.strip() or '(empty)'}"
        )
    verb = "wiped" if params.action == "wipe" else "GPT initialized"
    return (
        f"OK: `{params.disk}` {verb} via SSH{fallback_note}.\n"
        f"```\n{out.strip() or '(no output)'}\n```\n"
        "Verify with proxmox_list_disks."
    )


@mcp.tool(
    name="proxmox_disk_prepare",
    annotations={
        "title": "Prepare Disk: Wipe or Init GPT (DESTROYS DATA)",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_disk_prepare(params: DiskPrepareInput) -> str:
    """Wipe a disk or write a fresh GPT partition table.

    action='wipe' (confirm + i_understand_data_loss): erases the partition
    table and filesystem signatures — all data irretrievable. Disks in use by
    mounted FS / LVM / imported ZFS pools must be taken out of service first.

    action='init_gpt' (confirm): writes an empty GPT. The API refuses disks
    that already carry data — wipe first.

    via='auto' (default) uses the REST API and falls back to SSH
    (wipefs/sgdisk) when the endpoint rejects the API token.
    """
    cfg = require_config_or_ssh(params.via)
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm(f"proxmox_disk_prepare (action={params.action})")
    if params.action == "wipe" and not params.i_understand_data_loss:
        return missing_data_loss_ack("proxmox_disk_prepare (action=wipe)")

    if params.via == "ssh":
        return await _via_ssh(params)

    msg, exc = await _via_api(params)
    if msg is not None:
        return msg
    if params.via == "auto" and _is_token_auth_error(exc) and config.ssh_available():
        return await _via_ssh(
            params, fallback_note=" (API refused the token; fell back to SSH)"
        )
    return http_client.format_http_error(exc)


def require_config_or_ssh(via: str) -> Optional[str]:
    """API modes need the REST config; pure-SSH mode only needs SSH."""
    if via == "ssh":
        return config.require_ssh()
    return config.require_config()
