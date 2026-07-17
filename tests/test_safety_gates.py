"""Safety-gate bypass tests.

Proves the confirm / i_understand_data_loss / dry_run gates cannot be bypassed
by omission, typos, malformed values, or extra fields — and that a *refused*
mutation makes no HTTP write call. Companion to test_guest_create.py (which
covers the happy-path create/dry_run models).

Model validation is synchronous; tool calls are async and wrapped in
asyncio.run(), so this needs no pytest-asyncio. The mutating verb
(http_client.post) is monkeypatched with a spy so a refusal that still POSTed
would fail the test. Runnable with pytest or standalone.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import proxmox_mcp.config as _cfg  # noqa: E402

# Satisfy require_config() without a real .env; no test here reaches a real POST.
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

_ARCHIVE = "local:backup/vzdump-qemu-990-2026_01_01-00_00_00.vma.zst"


def _raises(model, **kwargs) -> bool:
    try:
        model(**kwargs)
    except ValidationError:
        return True
    return False


class _PostSpy:
    """Records whether the mutating HTTP verb was called."""

    def __init__(self) -> None:
        self.posts = 0

    async def post(self, path, data=None):  # matches http_client.post signature
        self.posts += 1
        return "UPID:spy"


def _patch_post():
    spy = _PostSpy()
    orig = http_client.post
    http_client.post = spy.post
    return spy, orig


def _patch_probe(existing: list):
    """Point the restore existence-probe (/cluster/resources) at a canned list."""
    orig = http_client.get

    async def fake_get(path, params=None):
        return existing

    http_client.get = fake_get
    return orig


# ---------- extra=forbid: a misspelled gate can't silently disable itself -----

def test_typo_gate_field_is_rejected_not_ignored():
    # 'confirmed' (typo) must RAISE — not be silently dropped, leaving
    # confirm=False while the caller believes they confirmed.
    assert _raises(
        CreateVmInput, node="pve", vmid=999, disk_storage="local-lvm",
        disk_gb=10, confirmed=True,
    )
    assert _raises(
        BackupRestoreInput, node="pve", vmid=990, archive=_ARCHIVE,
        i_understand_dataloss=True,
    )


# ---------- malformed gate values are rejected, not coerced into a bypass -----

def test_malformed_confirm_values_rejected():
    base = dict(node="pve", vmid=999, disk_storage="local-lvm", disk_gb=10)
    assert _raises(CreateVmInput, **base, confirm="maybe")
    assert _raises(CreateVmInput, **base, confirm=2)
    assert _raises(CreateVmInput, **base, confirm=[])
    # a genuine truthy string is still accepted (Pydantic coerces "true" -> True)
    assert CreateVmInput(**base, confirm="true").confirm is True


def test_malformed_data_loss_values_rejected():
    base = dict(node="pve", vmid=990, vm_type="qemu", archive=_ARCHIVE)
    assert _raises(BackupRestoreInput, **base, i_understand_data_loss="yep")
    assert _raises(BackupRestoreInput, **base, i_understand_data_loss=3)


# ---------- gate omission -> no write call ------------------------------------

def test_create_vm_without_confirm_makes_no_post():
    async def _t():
        spy, orig = _patch_post()
        try:
            r = await proxmox_create_vm(CreateVmInput(
                node="pve", vmid=999, disk_storage="local-lvm", disk_gb=10))
        finally:
            http_client.post = orig
        assert "requires confirm=true" in r
        assert spy.posts == 0
    asyncio.run(_t())


def test_dry_run_makes_no_post_even_with_confirm():
    async def _t():
        spy, orig = _patch_post()
        try:
            r = await proxmox_create_container(CreateContainerInput(
                node="pve", vmid=998, ostemplate="local:vztmpl/deb.tar.zst",
                disk_storage="local-lvm", confirm=True, dry_run=True,
                password="supersecret"))
        finally:
            http_client.post = orig
        assert "DRY RUN" in r
        assert "supersecret" not in r  # masked even on a confirmed dry-run
        assert spy.posts == 0
    asyncio.run(_t())


# ---------- restore data-loss gate: force without ack -> refuse, no write -----

def test_restore_overwrite_without_ack_makes_no_post():
    async def _t():
        get_orig = _patch_probe(
            [{"vmid": 990, "node": "pve", "type": "qemu",
              "name": "old", "status": "running"}])
        spy, post_orig = _patch_post()
        try:
            r = await proxmox_restore_backup(BackupRestoreInput(
                node="pve", vmid=990, vm_type="qemu", archive=_ARCHIVE,
                force=True, confirm=True))  # force + confirm, but NO data-loss ack
        finally:
            http_client.get = get_orig
            http_client.post = post_orig
        assert "i_understand_data_loss=true" in r
        assert spy.posts == 0
    asyncio.run(_t())


def test_restore_existing_without_force_refuses():
    async def _t():
        get_orig = _patch_probe(
            [{"vmid": 990, "node": "pve", "type": "qemu",
              "name": "old", "status": "running"}])
        spy, post_orig = _patch_post()
        try:
            r = await proxmox_restore_backup(BackupRestoreInput(
                node="pve", vmid=990, vm_type="qemu", archive=_ARCHIVE,
                confirm=True))  # exists, no force
        finally:
            http_client.get = get_orig
            http_client.post = post_orig
        assert "already exists" in r
        assert spy.posts == 0
    asyncio.run(_t())


def test_restore_without_confirm_makes_no_post():
    async def _t():
        get_orig = _patch_probe([])  # no existing guest -> would be a fresh restore
        spy, post_orig = _patch_post()
        try:
            r = await proxmox_restore_backup(BackupRestoreInput(
                node="pve", vmid=991, vm_type="qemu",
                archive="local:backup/vzdump-qemu-991-2026_01_01-00_00_00.vma.zst"))
        finally:
            http_client.get = get_orig
            http_client.post = post_orig
        assert "requires confirm=true" in r
        assert spy.posts == 0
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
