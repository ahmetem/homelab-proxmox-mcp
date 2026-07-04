"""Security tests for the read-only host exec allow-list.

`validate_read_command` is the safety boundary for proxmox_host_read_exec — a
tool that can be granted to a read-only agent. These tests assert that safe
read commands pass and that every mutation/escape path is refused.

Runnable with pytest (`python -m pytest tests/`) or standalone
(`python tests/test_host_read.py`).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proxmox_mcp.tools.host_read import validate_read_command  # noqa: E402


def _ok(cmd):
    assert validate_read_command(cmd) is None, f"should ALLOW: {cmd!r}"


def _no(cmd):
    assert validate_read_command(cmd) is not None, f"should REFUSE: {cmd!r}"


def test_allows_plain_reads():
    _ok("cat /usr/local/sbin/pve-perl-crashwatch.sh")
    _ok("ls -la /var/log")
    _ok("head -n 50 /etc/pve/storage.cfg")
    _ok("tail -n 200 /var/log/syslog")
    _ok("du -sh /var/lib/vz")
    _ok("df -h")
    _ok("stat /etc/hosts")
    _ok("uptime")
    _ok("journalctl -u pvedaemon -n 100 --no-pager")


def test_allows_restricted_subcommands():
    _ok("zpool status nvmepool")
    _ok("zpool list")
    _ok("zfs list -t snapshot")
    _ok("zfs get compressratio nvmepool")
    _ok("systemctl status pvedaemon")
    _ok("systemctl is-active corosync")
    _ok("pct config 205")
    _ok("qm list")
    _ok("pvesm status")
    _ok("ip addr")


def test_allows_globs_and_quotes():
    _ok("cat /etc/pve/nodes/*/qemu-server/*.conf")
    _ok('cat "/var/log/path with space.log"')


def test_rejects_shell_metacharacters():
    _no("cat /etc/hosts; rm -rf /")
    _no("cat /etc/hosts && reboot")
    _no("cat /etc/hosts | tee /tmp/x")
    _no("cat /etc/hosts > /tmp/out")
    _no("cat /etc/hosts >> /tmp/out")
    _no("echo $(rm -rf /)")
    _no("cat `whoami`")
    _no("cat ${HOME}/x")
    _no("(rm -rf /tmp)")
    _no("rm {a,b}")
    _no("cat a\nrm b")
    _no("cat a & disown")


def test_rejects_non_allowlisted_binaries():
    _no("rm -rf /")
    _no("echo hello")           # echo not needed; not allow-listed
    _no("bash -c 'rm -rf /'")
    _no("sh")
    _no("dd if=/dev/zero of=/dev/sda")
    _no("wipefs -a /dev/sdb")
    _no("nano /etc/fstab")
    _no("vi /etc/fstab")


def test_rejects_mutating_subcommands():
    _no("systemctl restart pvedaemon")
    _no("systemctl stop corosync")
    _no("systemctl disable pve-ha-lrm")
    _no("zpool destroy nvmepool")
    _no("zpool scrub nvmepool")
    _no("zfs destroy nvmepool/subvol-205-disk-0")
    _no("zfs set compression=off nvmepool")
    _no("pct exec 205 -- rm -rf /")
    _no("pct stop 205")
    _no("qm stop 101")
    _no("pvesm remove local")


def test_rejects_hanging_and_state_changing_flags():
    _no("tail -f /var/log/syslog")
    _no("tail --follow /var/log/syslog")
    _no("journalctl -f")
    _no("journalctl --rotate")
    _no("journalctl --vacuum-time=1s")
    _no("journalctl --flush")
    _no("smartctl -t short /dev/sda")
    _no("smartctl --test=long /dev/sda")


def test_allows_new_read_utilities():
    _ok("pvs")
    _ok("lvs -o +copy_percent pve")
    _ok("vgs pve")
    _ok("lvdisplay /dev/pve/root")
    _ok("lspci -nn")
    _ok("lsof -p 1")
    _ok("dmesg")
    _ok("dmesg -T")
    _ok("zdb -C nvmepool")
    _ok("grep -n root /etc/passwd")
    _ok("zcat /var/log/syslog.2.gz")
    _ok("dpkg-query -l")
    _ok("apt-cache policy pve-manager")
    _ok("dmidecode -t memory")


def test_allows_new_restricted_subcommands():
    _ok("proxmox-boot-tool status")
    _ok("nvme list")
    _ok("nvme smart-log /dev/nvme0n1")
    _ok("apt list --upgradable")
    _ok("apt-mark showhold")
    _ok("chronyc tracking")
    _ok("coredumpctl list --since=-15min")
    _ok("systemctl list-jobs")
    _ok("zpool version")
    _ok("qm pending 101")


def test_rejects_new_mutating_and_write_flags():
    # dmesg clear / console / follow
    _no("dmesg -C")
    _no("dmesg --clear")
    _no("dmesg -n 1")
    _no("dmesg --console-level=1")
    _no("dmesg -w")
    # dmidecode dump-to-file
    _no("dmidecode --dump-bin /tmp/smbios.bin")
    _no("dmidecode --dump")
    # nvme mutating subcommands
    _no("nvme format /dev/nvme0n1")
    _no("nvme sanitize /dev/nvme0n1")
    _no("nvme set-feature /dev/nvme0n1 -f 6 -v 0")
    # apt / apt-mark mutations
    _no("apt install htop")
    _no("apt full-upgrade")
    _no("apt-mark hold pve-manager")
    # others
    _no("chronyc makestep")
    _no("proxmox-boot-tool init /dev/sdd2")
    _no("coredumpctl dump 1234")
    _no("pvcreate /dev/sdd3")     # LVM mutators are not allow-listed at all
    _no("lvremove pve/data")
    _no("vgreduce pve /dev/sdc3")


def test_rejects_empty():
    _no("")
    _no("   ")


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
