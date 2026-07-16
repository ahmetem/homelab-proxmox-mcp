"""Tests for guest creation (create_vm / create_container) and the dry_run
preview added to create / clone / restore.

Model validation is synchronous; the dry_run paths are async but wrapped in
asyncio.run() so this needs no pytest-asyncio. create/clone dry_run make no
HTTP call; restore dry_run does one read-only existence probe, which is
monkeypatched here. Runnable with pytest or standalone.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import proxmox_mcp.config as _cfg  # noqa: E402

# Satisfy require_config() without a real .env (create/clone dry_run make no
# HTTP call; restore's probe is monkeypatched). Keep a real .env if present.
_cfg.PROXMOX_HOST = _cfg.PROXMOX_HOST or "192.0.2.1"
_cfg.PROXMOX_USER = _cfg.PROXMOX_USER or "root@pam"
_cfg.PROXMOX_TOKEN_NAME = _cfg.PROXMOX_TOKEN_NAME or "t"
_cfg.PROXMOX_TOKEN_VALUE = _cfg.PROXMOX_TOKEN_VALUE or "x"

from pydantic import ValidationError  # noqa: E402

from proxmox_mcp import http_client  # noqa: E402
from proxmox_mcp.models import BackupRestoreInput  # noqa: E402
from proxmox_mcp.tools.backups import proxmox_restore_backup  # noqa: E402
from proxmox_mcp.tools.guest_create import (  # noqa: E402
    CreateContainerInput,
    CreateVmInput,
    proxmox_create_container,
    proxmox_create_vm,
)
from proxmox_mcp.tools.vm_disk import CloneVmInput, proxmox_clone_vm  # noqa: E402


def _raises(model, **kwargs):
    try:
        model(**kwargs)
    except ValidationError:
        return True
    return False


# ---------- model validation ----------

def test_create_vm_required_and_ranges():
    CreateVmInput(node="pve", vmid=999, disk_storage="local-lvm", disk_gb=10)
    assert _raises(CreateVmInput, node="pve", vmid=999, disk_storage="local-lvm")
    assert _raises(CreateVmInput, node="pve", vmid=99, disk_storage="local-lvm", disk_gb=10)
    assert _raises(CreateVmInput, node="pve", vmid=999, disk_storage="local-lvm",
                   disk_gb=10, bogus=1)
    assert _raises(CreateVmInput, node="pve", vmid=999, disk_storage="local-lvm",
                   disk_gb=10, ostype="freebsd")
    assert _raises(CreateVmInput, node="pve", vmid=999, disk_storage="bad storage",
                   disk_gb=10)


def test_create_container_required_and_ip_octets():
    CreateContainerInput(node="pve", vmid=998,
                         ostemplate="local:vztmpl/deb.tar.zst", disk_storage="local-lvm")
    CreateContainerInput(node="pve", vmid=998, ostemplate="local:vztmpl/deb.tar.zst",
                         disk_storage="local-lvm", ip="192.168.1.50/24",
                         gateway="192.168.1.1")
    assert _raises(CreateContainerInput, node="pve", vmid=998,
                   ostemplate="local:vztmpl/deb.tar.zst", disk_storage="local-lvm",
                   gateway="192.168.1.1")  # gateway with default dhcp ip -> rejected
    assert _raises(CreateContainerInput, node="pve", vmid=998, disk_storage="local-lvm")
    assert _raises(CreateContainerInput, node="pve", vmid=998,
                   ostemplate="local:vztmpl/x.tar.zst", disk_storage="local-lvm",
                   ip="999.1.1.1/24")
    assert _raises(CreateContainerInput, node="pve", vmid=998,
                   ostemplate="local:vztmpl/x.tar.zst", disk_storage="local-lvm",
                   password="123")


# ---------- dry_run previews ----------

def test_create_vm_dry_run_previews_without_http():
    async def _t():
        r = await proxmox_create_vm(CreateVmInput(
            node="pve", vmid=999, name="t", disk_storage="local-lvm", disk_gb=10,
            iso="local:iso/deb.iso", dry_run=True))
        assert "DRY RUN" in r
        assert '"scsi0":"local-lvm:10"' in r
        assert "local:iso/deb.iso,media=cdrom" in r
    asyncio.run(_t())


def test_create_container_dry_run_masks_secrets():
    async def _t():
        r = await proxmox_create_container(CreateContainerInput(
            node="pve", vmid=998, ostemplate="local:vztmpl/deb.tar.zst",
            disk_storage="local-lvm", password="supersecret",
            ssh_public_key="ssh-ed25519 KEYDATA", dry_run=True))
        assert "DRY RUN" in r and "***" in r
        assert "supersecret" not in r and "KEYDATA" not in r
    asyncio.run(_t())


def test_create_requires_confirm():
    async def _t():
        r = await proxmox_create_vm(CreateVmInput(
            node="pve", vmid=999, disk_storage="local-lvm", disk_gb=10))
        assert "requires confirm=true" in r
    asyncio.run(_t())


def test_clone_dry_run():
    async def _t():
        r = await proxmox_clone_vm(CloneVmInput(
            node="pve", vmid=100, newid=200, full=True, dry_run=True))
        assert "DRY RUN" in r and '"newid":200' in r
    asyncio.run(_t())


def test_restore_dry_run_reports_effect():
    async def _t():
        orig = http_client.get

        async def fake_get(path, params=None):
            return []  # no existing guests -> fresh restore, no real HTTP

        http_client.get = fake_get
        try:
            r = await proxmox_restore_backup(BackupRestoreInput(
                node="pve", vmid=990, vm_type="qemu",
                archive="local:backup/vzdump-qemu-990-2026_01_01-00_00_00.vma.zst",
                dry_run=True))
        finally:
            http_client.get = orig
        assert "DRY RUN" in r and "fresh restore" in r
    asyncio.run(_t())


def _run_standalone():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_standalone())
