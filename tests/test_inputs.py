"""Validation tests for the consolidated action-based input models.

These guard the v1.0 consolidation: each merged tool routes on an `action`
field, and per-action required parameters are enforced by model validators.
Runnable with pytest or standalone (`python tests/test_inputs.py`).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydantic import ValidationError  # noqa: E402

from proxmox_mcp.models import SnapshotManageInput, VMListInput, VMPowerInput  # noqa: E402
from proxmox_mcp.tools.lvm_manage import LvmManageInput  # noqa: E402
from proxmox_mcp.tools.zfs_manage import ZfsPoolManageInput  # noqa: E402
from proxmox_mcp.tools.disks_prepare import DiskPrepareInput  # noqa: E402
from proxmox_mcp.tools.storage_manage import StorageConfigInput  # noqa: E402
from proxmox_mcp.tools.ssh_zfs import ZfsDatasetInput  # noqa: E402
from proxmox_mcp.tools.ssh_zfs_phase3 import ZfsPropertyInput  # noqa: E402


def _raises(model, **kwargs):
    try:
        model(**kwargs)
    except ValidationError:
        return True
    return False


def test_vm_power_action_pattern():
    VMPowerInput(node="pve", vmid=101, action="start")
    assert _raises(VMPowerInput, node="pve", vmid=101, action="suspend")


def test_vm_list_filters_validated():
    VMListInput(status="running", guest_type="lxc")
    assert _raises(VMListInput, status="paused")
    assert _raises(VMListInput, guest_type="docker")


def test_snapshot_action_pattern():
    SnapshotManageInput(action="create", node="pve", vmid=101, snapname="test1")
    assert _raises(SnapshotManageInput, action="clone", node="pve", vmid=101,
                   snapname="test1")


def test_lvm_create_requires_device():
    LvmManageInput(action="create_vg", node="pve", name="vg0",
                   device="/dev/sdb")
    assert _raises(LvmManageInput, action="create_vg", node="pve", name="vg0")


def test_lvm_destroy_thin_requires_vg():
    LvmManageInput(action="destroy_thin", node="pve", name="pool0",
                   volume_group="vg0")
    assert _raises(LvmManageInput, action="destroy_thin", node="pve",
                   name="pool0")


def test_zfs_pool_create_requires_devices():
    ZfsPoolManageInput(action="create", node="pve", name="tank",
                       devices=["/dev/sdb"])
    ZfsPoolManageInput(action="destroy", node="pve", name="tank")
    assert _raises(ZfsPoolManageInput, action="create", node="pve", name="tank")
    assert _raises(ZfsPoolManageInput, action="create", node="pve", name="tank",
                   devices=["sdb"])  # must start with /dev/


def test_disk_prepare_action_and_via():
    DiskPrepareInput(action="wipe", node="pve", disk="/dev/sdb")
    assert _raises(DiskPrepareInput, action="format", node="pve", disk="/dev/sdb")
    assert _raises(DiskPrepareInput, action="wipe", node="pve", disk="/dev/sdb",
                   via="teleport")


def test_storage_config_per_action_requirements():
    StorageConfigInput(action="add_zfs", storage="tank", pool="tank")
    StorageConfigInput(action="add_dir", storage="isos", path="/mnt/isos")
    StorageConfigInput(action="remove", storage="tank")
    assert _raises(StorageConfigInput, action="add_zfs", storage="tank")
    assert _raises(StorageConfigInput, action="add_dir", storage="isos")


def test_zfs_dataset_validates_name_and_props():
    ZfsDatasetInput(action="create", name="nvmepool/data",
                    properties={"compression": "lz4"})
    assert _raises(ZfsDatasetInput, action="create", name="nvmepool/data",
                   properties={"not_allowed_prop": "x"})
    assert _raises(ZfsDatasetInput, action="create", name="bad name")


def test_zfs_property_value_charset():
    ZfsPropertyInput(name="nvmepool", property="atime", action="set", value="off")
    assert _raises(ZfsPropertyInput, name="nvmepool", property="atime",
                   action="set", value="off; rm -rf /")


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
