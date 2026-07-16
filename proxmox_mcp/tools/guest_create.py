"""Guest creation: build QEMU VMs and LXC containers from scratch.

Backed by Proxmox REST endpoints:
  - POST /nodes/{node}/qemu   (create VM)
  - POST /nodes/{node}/lxc    (create container)

These are the "from scratch" counterparts to proxmox_clone_vm. Scope is
deliberately *medium*: core hardware + access (CT ssh-key/password) + optional
install media (VM ISO). Cloud-init, multi-disk and multi-NIC are intentionally
left out for now — add a disk/NIC afterwards with the config/resize tools.

Safety: both require confirm=true. Neither destroys data — Proxmox refuses to
create over an existing VMID (the endpoint returns an error), so there is no
i_understand_data_loss gate. Pass dry_run=true to preview the exact endpoint
and payload (secrets masked) without creating anything.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import dry_run_preview, missing_confirm
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import WAIT_DESC


# Storage id, network bridge, hostname/name charsets.
_STORAGE_ID = r"^[A-Za-z][A-Za-z0-9_.-]*$"
_BRIDGE = r"^[A-Za-z][A-Za-z0-9_.-]*$"
_HOSTNAME = r"^[A-Za-z0-9][A-Za-z0-9.-]*$"
# volid like 'local:iso/debian-12.iso' or 'local:vztmpl/debian-12.tar.zst'
_VOLID = r"^[A-Za-z0-9][A-Za-z0-9_.-]*:[A-Za-z0-9][\w./+-]*$"
# IPv4 with real 0-255 octets (so '999.1.1.1' is rejected, not just malformed).
_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])"
_IPV4 = r"^" + _OCTET + r"(?:\." + _OCTET + r"){3}$"
# 'dhcp', 'manual', or a static CIDR like '192.168.1.50/24' (mask 0-32).
_CT_IP = (
    r"^(?:dhcp|manual|"
    + _OCTET + r"(?:\." + _OCTET + r"){3}/(?:3[0-2]|[12]?[0-9]))$"
)


class CreateVmInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., min_length=1, description="Node to create the VM on (e.g. 'pve').")
    vmid: int = Field(
        ..., ge=100, le=999999999,
        description="New VM ID. Must not already exist (Proxmox refuses to overwrite).",
    )
    name: Optional[str] = Field(
        default=None, description="VM name.",
        min_length=1, max_length=64, pattern=_HOSTNAME,
    )
    cores: int = Field(default=2, ge=1, le=256, description="CPU cores per socket.")
    sockets: int = Field(default=1, ge=1, le=8)
    memory_mb: int = Field(default=2048, ge=16, le=4194304, description="RAM in MB.")
    ostype: str = Field(
        default="l26",
        description="OS type: 'l26' (modern Linux), 'win11', 'win10', 'other', etc.",
        pattern=r"^(other|wxp|w2k|w2k3|w2k8|wvista|win7|win8|win10|win11|l24|l26|solaris)$",
    )
    disk_storage: str = Field(
        ..., max_length=64, pattern=_STORAGE_ID,
        description="Storage for the boot disk (e.g. 'local-lvm', 'nvmepool').",
    )
    disk_gb: int = Field(
        ..., ge=1, le=65536,
        description="Boot disk size in GB (created as scsi0 on virtio-scsi).",
    )
    bridge: str = Field(
        default="vmbr0", max_length=32, pattern=_BRIDGE,
        description="Network bridge for net0.",
    )
    iso: Optional[str] = Field(
        default=None, max_length=256, pattern=_VOLID,
        description=(
            "Optional install ISO volid from proxmox_list_isos "
            "(e.g. 'local:iso/debian-12.iso'). Attached as CD-ROM; boot order "
            "prefers the CD so the installer runs first."
        ),
    )
    start: bool = Field(default=False, description="Power on the VM right after creation.")
    confirm: bool = Field(default=False)
    dry_run: bool = Field(
        default=False,
        description="Preview the endpoint + payload (secrets masked) without creating.",
    )
    wait_seconds: int = Field(default=0, ge=0, le=600, description=WAIT_DESC)
    reason: Optional[str] = Field(default=None, max_length=200)


class CreateContainerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(..., min_length=1, description="Node to create the container on.")
    vmid: int = Field(
        ..., ge=100, le=999999999,
        description="New container ID. Must not already exist.",
    )
    ostemplate: str = Field(
        ..., min_length=3, max_length=256, pattern=_VOLID,
        description=(
            "Template volid, e.g. "
            "'local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst'. "
            "List available templates with content=vztmpl on a storage."
        ),
    )
    hostname: Optional[str] = Field(
        default=None, min_length=1, max_length=64, pattern=_HOSTNAME,
        description="Container hostname.",
    )
    cores: int = Field(default=1, ge=1, le=256)
    memory_mb: int = Field(default=512, ge=16, le=4194304, description="RAM in MB.")
    swap_mb: int = Field(default=512, ge=0, le=4194304, description="Swap in MB.")
    disk_storage: str = Field(
        ..., max_length=64, pattern=_STORAGE_ID,
        description="Storage for the root filesystem (e.g. 'local-lvm').",
    )
    disk_gb: int = Field(
        default=8, ge=1, le=65536, description="Root filesystem size in GB (rootfs).",
    )
    bridge: str = Field(default="vmbr0", max_length=32, pattern=_BRIDGE)
    ip: str = Field(
        default="dhcp", max_length=32, pattern=_CT_IP,
        description="'dhcp', 'manual', or a static CIDR like '192.168.1.50/24'.",
    )
    gateway: Optional[str] = Field(
        default=None, max_length=15, pattern=_IPV4,
        description="Gateway IP for a static config (ignored when ip='dhcp').",
    )
    password: Optional[str] = Field(
        default=None, min_length=5, max_length=128,
        description="Root password (>=5 chars). Prefer ssh_public_key. Never logged.",
    )
    ssh_public_key: Optional[str] = Field(
        default=None, max_length=4096,
        description="SSH public key(s) for root, one per line.",
    )
    unprivileged: bool = Field(default=True, description="Unprivileged container (recommended).")
    nesting: bool = Field(
        default=False,
        description="Enable the nesting feature (needed for e.g. Docker-in-LXC).",
    )
    start: bool = Field(default=False, description="Start the container right after creation.")
    confirm: bool = Field(default=False)
    dry_run: bool = Field(
        default=False,
        description="Preview the endpoint + payload (secrets masked) without creating.",
    )
    wait_seconds: int = Field(default=0, ge=0, le=600, description=WAIT_DESC)
    reason: Optional[str] = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _gateway_needs_static_ip(self):
        # A gateway is meaningless with dhcp/manual and would be silently
        # dropped; reject it up front instead of misconfiguring the guest.
        if self.gateway and self.ip in ("dhcp", "manual"):
            raise ValueError("gateway requires a static ip (CIDR), not dhcp/manual.")
        return self


@mcp.tool(
    name="proxmox_create_vm",
    annotations={
        "title": "Create QEMU VM (from scratch)",
        "readOnlyHint": False, "destructiveHint": False,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_create_vm(params: CreateVmInput) -> str:
    """Create a new QEMU VM with core hardware and an optional install ISO.

    Builds one boot disk (scsi0 on virtio-scsi-single), one NIC (virtio on the
    given bridge), and — if `iso` is set — a CD-ROM the VM boots from first so
    an OS installer can run. For cloud images / cloud-init, or extra disks and
    NICs, add them afterwards.

    Requires confirm=true. Pass dry_run=true to preview without creating.
    """
    cfg = require_config()
    if cfg:
        return cfg

    path = f"/nodes/{params.node}/qemu"
    payload: dict = {
        "vmid": params.vmid,
        "cores": params.cores,
        "sockets": params.sockets,
        "memory": params.memory_mb,
        "ostype": params.ostype,
        "scsihw": "virtio-scsi-single",
        "scsi0": f"{params.disk_storage}:{params.disk_gb}",
        "net0": f"virtio,bridge={params.bridge}",
    }
    if params.name:
        payload["name"] = params.name
    if params.iso:
        payload["ide2"] = f"{params.iso},media=cdrom"
        payload["boot"] = "order=ide2;scsi0"
    else:
        payload["boot"] = "order=scsi0"
    if params.start:
        payload["start"] = 1

    if params.dry_run:
        return dry_run_preview("POST", path, payload)
    if not params.confirm:
        return missing_confirm("proxmox_create_vm")

    try:
        task_id = await http_client.post(path, data=payload)
    except Exception as exc:
        return http_client.format_http_error(exc)

    suffix = await http_client.wait_for_task(params.node, task_id, params.wait_seconds)
    if not suffix:
        suffix = " Use proxmox_get_vm_status to check it."
    boot_note = " Boots from the ISO first." if params.iso else ""
    start_note = " Powering on." if params.start else ""
    return (
        f"OK: VM {params.vmid} created on `{params.node}` — {params.cores}x"
        f"{params.sockets} cores, {params.memory_mb} MB RAM, "
        f"{params.disk_gb} GB on `{params.disk_storage}`, net on "
        f"`{params.bridge}`.{boot_note}{start_note} Task: {task_id}.{suffix}"
    )


@mcp.tool(
    name="proxmox_create_container",
    annotations={
        "title": "Create LXC Container (from scratch)",
        "readOnlyHint": False, "destructiveHint": False,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_create_container(params: CreateContainerInput) -> str:
    """Create a new LXC container from a template.

    Builds a rootfs of the given size, one NIC (eth0 on the given bridge, dhcp
    by default), and sets root access via password and/or ssh_public_key.
    Unprivileged by default. Enable `nesting` for Docker-in-LXC and similar.

    Requires confirm=true. Pass dry_run=true to preview without creating.
    Secrets (password, ssh key) are never written to logs or previews.
    """
    cfg = require_config()
    if cfg:
        return cfg

    net0 = f"name=eth0,bridge={params.bridge},ip={params.ip}"
    if params.gateway and params.ip not in ("dhcp", "manual"):
        net0 += f",gw={params.gateway}"

    path = f"/nodes/{params.node}/lxc"
    payload: dict = {
        "vmid": params.vmid,
        "ostemplate": params.ostemplate,
        "cores": params.cores,
        "memory": params.memory_mb,
        "swap": params.swap_mb,
        "rootfs": f"{params.disk_storage}:{params.disk_gb}",
        "net0": net0,
        "unprivileged": 1 if params.unprivileged else 0,
    }
    if params.hostname:
        payload["hostname"] = params.hostname
    if params.password:
        payload["password"] = params.password
    if params.ssh_public_key:
        payload["ssh-public-keys"] = params.ssh_public_key
    if params.nesting:
        payload["features"] = "nesting=1"
    if params.start:
        payload["start"] = 1

    if params.dry_run:
        return dry_run_preview("POST", path, payload)
    if not params.confirm:
        return missing_confirm("proxmox_create_container")

    try:
        task_id = await http_client.post(path, data=payload)
    except Exception as exc:
        return http_client.format_http_error(exc)

    suffix = await http_client.wait_for_task(params.node, task_id, params.wait_seconds)
    if not suffix:
        suffix = " Use proxmox_get_vm_status (vm_type=lxc) to check it."
    priv = "unprivileged" if params.unprivileged else "PRIVILEGED"
    start_note = " Starting." if params.start else ""
    return (
        f"OK: CT {params.vmid} created on `{params.node}` ({priv}) — "
        f"{params.cores} cores, {params.memory_mb} MB RAM, "
        f"{params.disk_gb} GB rootfs on `{params.disk_storage}`, net "
        f"`{params.ip}` on `{params.bridge}`.{start_note} Task: {task_id}.{suffix}"
    )
