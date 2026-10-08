"""Gate: no raw `zfs destroy` of a snapshot registered in a PVE guest config.

Covers proxmox_host_exec, proxmox_zfs_destroy_snapshots_by_pattern and
proxmox_cleanup_vzdump_snapshots. SSH is replaced with spies that serve canned
config headers / `zfs list` output and record every destroy, so a refusal that
still destroyed something fails the test. Runnable with pytest or standalone.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import proxmox_mcp.config as _cfg  # noqa: E402

_cfg.PROXMOX_SSH_HOST = _cfg.PROXMOX_SSH_HOST or "192.0.2.1"
_cfg.PROXMOX_SSH_KEY_PATH = _cfg.PROXMOX_SSH_KEY_PATH or "/nonexistent/key"

from proxmox_mcp import host_ssh, pve_snapshots, ssh  # noqa: E402
from proxmox_mcp.tools.backup_maint import (  # noqa: E402
    CleanupVzdumpInput,
    proxmox_cleanup_vzdump_snapshots,
)
from proxmox_mcp.tools.host_ssh import HostExecInput, proxmox_host_exec  # noqa: E402
from proxmox_mcp.tools.ssh_zfs import (  # noqa: E402
    ZfsDestroySnapshotsByPatternInput,
    proxmox_zfs_destroy_snapshots_by_pattern,
)

# Shape verified against the live host (grep -H '^\[' on /etc/pve/lxc/200.conf).
_CONF = """\
/etc/pve/lxc/200.conf:[autoupd_n8n_20261008_0507]
/etc/pve/lxc/200.conf:[daily_20261008]
/etc/pve/lxc/200.conf:[vzdump]
/etc/pve/lxc/207.conf:[pre-rename-sys]
/etc/pve/lxc/207.conf:[PENDING]
/etc/pve/qemu-server/101.conf:[pre_upgrade]
"""

# The 2026-10-05 command that caused the outage (shape from _host_ssh_audit.log).
_INCIDENT = (
    "P=nvmepool; for s in subvol-208-disk-0@pre_debian13_20260729 "
    "subvol-207-disk-0@pre_debian13_20260729,pre-node22-20260905,pre-rename-sys "
    "subvol-200-disk-0@daily_20261008; do zfs destroy $P/$s && echo ok $s; done"
)


class _Host:
    """Fake host: answers the config read, records every other command."""

    def __init__(self, conf=_CONF, conf_rc=0, extra=None):
        self.conf, self.conf_rc, self.extra = conf, conf_rc, extra or {}
        self.ran: list[str] = []
        self.audit: list[str] = []

    async def exec_command(self, cmd, *, timeout=60.0):
        if cmd == pve_snapshots.READ_CMD:
            return self.conf_rc, self.conf if self.conf_rc == 0 else "", ""
        self.ran.append(cmd)
        return 0, self.extra.get(cmd, ""), ""

    def audit_log(self, cmd, rc, *, note="", **_kw):
        self.audit.append(note)


def _with_host(fake, coro_fn):
    orig = host_ssh.exec_command, host_ssh.audit_log
    host_ssh.exec_command, host_ssh.audit_log = fake.exec_command, fake.audit_log
    try:
        return asyncio.run(coro_fn())
    finally:
        host_ssh.exec_command, host_ssh.audit_log = orig


def _host_exec(cmd, fake=None, **kw):
    fake = fake or _Host()
    params = HostExecInput(command=cmd, confirm=True, i_understand_data_loss=True, **kw)
    return _with_host(fake, lambda: proxmox_host_exec(params)), fake


# ---------- pure helpers ------------------------------------------------------

def test_parse_registered_skips_non_snapshot_sections():
    reg = pve_snapshots.parse_registered(_CONF)
    assert reg[200] == {"autoupd_n8n_20261008_0507", "daily_20261008", "vzdump"}
    assert reg[207] == {"pre-rename-sys"}  # PENDING is not a snapshot
    assert reg[101] == {"pre_upgrade"}


def test_vmid_of():
    assert pve_snapshots.vmid_of("nvmepool/subvol-200-disk-0") == 200
    assert pve_snapshots.vmid_of("vm-101-disk-1") == 101
    assert pve_snapshots.vmid_of("nvmepool/data") is None


# ---------- proxmox_host_exec -------------------------------------------------

def test_host_exec_registered_snapshot_refused():
    r, fake = _host_exec("zfs destroy nvmepool/subvol-200-disk-0@daily_20261008")
    assert r.startswith("Refused"), r
    assert "pct delsnapshot 200 daily_20261008" in r
    assert fake.ran == []
    assert fake.audit == ["REFUSED pve-registered-snapshot"]


def test_host_exec_vm_snapshot_points_to_qm():
    r, fake = _host_exec("zfs destroy nvmepool/vm-101-disk-0@pre_upgrade")
    assert "qm delsnapshot 101 pre_upgrade" in r and fake.ran == []


def test_host_exec_comma_list_with_one_registered_refused():
    r, fake = _host_exec("zfs destroy nvmepool/subvol-207-disk-0@old_a,pre-rename-sys")
    assert "pct delsnapshot 207 pre-rename-sys" in r and fake.ran == []


def test_host_exec_incident_command_refused():
    r, fake = _host_exec(_INCIDENT)
    assert r.startswith("Refused"), r
    assert "pct delsnapshot 207 pre-rename-sys" in r
    assert "pct delsnapshot 200 daily_20261008" in r
    assert "pre_debian13_20260729" not in r  # unregistered ones are not blamed
    assert fake.ran == []


def test_host_exec_autosnap_allowed():
    cmd = "zfs destroy nvmepool/subvol-200-disk-0@autosnap_2026-10-01_00:00:01_daily"
    r, fake = _host_exec(cmd)
    assert fake.ran == [cmd], r


def test_host_exec_unregistered_snapshot_allowed():
    cmd = "zfs destroy nvmepool/subvol-200-disk-0@pre_debian13_20260729"
    r, fake = _host_exec(cmd)
    assert fake.ran == [cmd], r


def test_host_exec_non_guest_dataset_allowed_even_if_name_collides():
    cmd = "zfs destroy nvmepool/data@daily_20261008"
    r, fake = _host_exec(cmd)
    assert fake.ran == [cmd], r


def test_host_exec_variable_dataset_checked_against_all_guests():
    r, fake = _host_exec("for d in $DS; do zfs destroy $d@daily_20261008; done")
    assert "pct delsnapshot 200 daily_20261008" in r and fake.ran == []


def test_host_exec_pool_root_recursive_checked_against_all_guests():
    r, fake = _host_exec("zfs destroy -r nvmepool@pre-rename-sys")
    assert "207" in r and fake.ran == []


def test_host_exec_range_on_guest_with_snapshots_refused():
    r, fake = _host_exec("zfs destroy nvmepool/subvol-200-disk-0@a%z")
    assert r.startswith("Refused") and fake.ran == []


def test_host_exec_config_read_failure_fails_closed():
    r, fake = _host_exec(
        "zfs destroy nvmepool/subvol-200-disk-0@autosnap_x", fake=_Host(conf_rc=1))
    assert r.startswith("Refused") and "could not be checked" in r
    assert fake.ran == []


def test_host_exec_without_zfs_destroy_skips_config_read():
    fake = _Host(conf_rc=1)  # a config read would fail closed — must not happen
    r, fake = _host_exec("ssh root@pam true; zfs list -t snapshot", fake=fake)
    assert fake.ran == ["ssh root@pam true; zfs list -t snapshot"], r


# ---------- proxmox_zfs_destroy_snapshots_by_pattern --------------------------

class _Zfs:
    def __init__(self, names):
        self.listing = "".join(f"{n}\t1790000000\t1024\n" for n in names)
        self.destroyed: list[str] = []

    async def run_command(self, argv, timeout=None):
        if argv[:2] == ["zfs", "list"]:
            return 0, self.listing, ""
        if argv[:2] == ["zfs", "destroy"]:
            self.destroyed.append(argv[2])
            return 0, "", ""
        raise AssertionError(f"unexpected argv {argv}")


def _pattern(names, pattern, dry_run=False):
    z = _Zfs(names)
    orig = ssh.run_command
    ssh.run_command = z.run_command
    try:
        params = ZfsDestroySnapshotsByPatternInput(
            dataset="nvmepool", pattern=pattern, recursive=True, dry_run=dry_run,
            confirm=True, i_understand_data_loss=True)
        r = _with_host(_Host(), lambda: proxmox_zfs_destroy_snapshots_by_pattern(params))
    finally:
        ssh.run_command = orig
    return r, z


def test_pattern_batch_with_registered_snapshot_refused_whole():
    r, z = _pattern(
        ["nvmepool/subvol-200-disk-0@daily_20261008",
         "nvmepool/subvol-201-disk-0@daily_20261008"],  # 201 not registered here
        "daily_*")
    assert "pct delsnapshot 200 daily_20261008" in r
    assert "201" not in r.split("Refused", 1)[1]
    assert z.destroyed == []


def test_pattern_autosnap_destroyed():
    names = ["nvmepool/subvol-200-disk-0@autosnap_2026-10-01_00:00:01_daily",
             "nvmepool/vm-101-disk-0@autosnap_2026-10-01_00:00:01_daily"]
    r, z = _pattern(names, "autosnap_*")
    assert z.destroyed == names, r


def test_pattern_unregistered_destroyed():
    names = ["nvmepool/subvol-208-disk-0@pre_debian13_20260729"]
    r, z = _pattern(names, "pre_debian13_*")
    assert z.destroyed == names, r


def test_pattern_dry_run_warns_about_registered():
    r, z = _pattern(["nvmepool/subvol-200-disk-0@daily_20261008"], "daily_*", dry_run=True)
    assert "A real run will refuse" in r and z.destroyed == []


# ---------- proxmox_cleanup_vzdump_snapshots ----------------------------------

def test_cleanup_vzdump_skips_registered_vzdump():
    listing = (
        "nvmepool/subvol-200-disk-0@vzdump\t1\n"  # [vzdump] in 200.conf
        "nvmepool/subvol-201-disk-0@vzdump\t1\n"
    )
    fake = _Host(extra={"zfs list -t snapshot -H -p -o name,creation": listing})
    params = CleanupVzdumpInput(dry_run=False, confirm=True, min_age_minutes=0)
    r = _with_host(fake, lambda: proxmox_cleanup_vzdump_snapshots(params))
    destroys = [c for c in fake.ran if c.startswith("zfs destroy")]
    assert destroys == ["zfs destroy nvmepool/subvol-201-disk-0@vzdump"], r
    assert "pct delsnapshot 200 vzdump" in r

    dry = _Host(extra={"zfs list -t snapshot -H -p -o name,creation": listing})
    r = _with_host(dry, lambda: proxmox_cleanup_vzdump_snapshots(
        CleanupVzdumpInput(min_age_minutes=0)))
    assert "Would remove 1 " in r and "pct delsnapshot 200 vzdump" in r, r
    assert not [c for c in dry.ran if c.startswith("zfs destroy")]


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
