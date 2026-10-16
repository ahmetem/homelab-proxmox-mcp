"""Security tests for the read-only host exec allow-list (madde 309).

`validate_read_command` (-> `proxmox_mcp.read_allow`) is the safety boundary for
proxmox_host_read_exec, a tool granted to read-only agents. Only listed programs,
subcommands and exact option spellings pass; every finding of the deny-list era
(madde 295, 310, 312, 316, 317) is a regression input here.

Runnable with pytest (`python -m pytest tests/`) or standalone
(`python tests/test_host_read.py`).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from proxmox_mcp import read_allow  # noqa: E402
from proxmox_mcp.tools.host_read import validate_read_command  # noqa: E402

# The shared file's fingerprint (CRLF folded to LF). homelab-agent-sys pins the SAME
# value in tests/test_read_allow.py; change both when read_allow.py changes, and copy
# the file byte for byte (madde 309).
READ_ALLOW_SHA256 = "21a9b7a0aadf1841c95994b5ae4a47176d5b7af3d96edfacc48866267cf5b641"


def _ok(cmd):
    assert validate_read_command(cmd) is None, f"should ALLOW: {cmd!r}: {validate_read_command(cmd)}"


def _no(cmd):
    assert validate_read_command(cmd) is not None, f"should REFUSE: {cmd!r}"


def test_shared_file_is_the_pinned_copy():
    path = os.path.join(os.path.dirname(_HERE), "proxmox_mcp", "read_allow.py")
    with open(path, "rb") as f:
        got = hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()
    assert got == READ_ALLOW_SHA256, (
        f"read_allow.py sha256 {got} != pinned {READ_ALLOW_SHA256}: copy the file to "
        "homelab-agent-sys and move the constant in BOTH repos")


def test_allows_plain_reads():
    for cmd in (
        "cat /usr/local/sbin/pve-perl-crashwatch.sh", "ls -la /var/log",
        "head -n 50 /etc/pve/storage.cfg", "tail -n 200 /var/log/syslog", "du -sh /var/lib/vz",
        "df -h", "stat /etc/hosts", "uptime", "journalctl -u pvedaemon -n 100 --no-pager",
        "free -m", "ps -eo pid,pcpu,comm --sort=-pcpu --no-headers", "ps aux", "uname -r",
        "pveversion -v", "lscpu", "lsblk -o NAME,SIZE", "findmnt -t zfs", "vmstat -f",
        "lsof -p 1 -a -i", "lsof -i :22", "pgrep -af pveproxy", "dmesg -T --level=err,warn",
        "dmidecode -t memory", "dpkg-query -W -f '${Version}' pve-manager", "zcat /var/log/syslog.2.gz",
        "grep -n root /etc/passwd", "grep -rhoE 'a|b' /etc", "grep -m1 -A3 -B2 x f", "grep -3 x f",
        "head -5 f", "tail -100 f", "tail -n +5 f", "du -d1 -xh /", "lvs -o +copy_percent pve",
        "/usr/bin/cat /etc/hosts", "/usr/sbin/zfs list",
    ):
        _ok(cmd)


def test_allows_restricted_subcommands():
    for cmd in (
        "zpool status nvmepool", "zpool status -x", "zpool list", "zpool iostat -v",
        "zfs list -t snapshot", "zfs list -H -o name -t snapshot -r vmpool", "zfs get compressratio nvmepool",
        "systemctl status pvedaemon", "systemctl is-active corosync", "systemctl --failed",
        "systemctl", "systemctl list-timers --all --no-pager", "systemctl show -p ActiveState x",
        "pct config 205", "pct config 205 --current", "pct status 216 --verbose", "pct list",
        "qm list", "qm config 101", "qm showcmd 101 --pretty", "qm pending 101",
        "pvesm status", "pvesm list local --content backup", "pvesh get /cluster/resources --type vm",
        "pvesh get /nodes --output-format json", "ip addr", "ip -br a", "ip route get 1.1.1.1",
        "proxmox-boot-tool status", "apt list --upgradable", "apt-mark showhold",
        "apt-cache policy pve-manager", "chronyc tracking", "chronyc -n sources",
        "coredumpctl list --since=-15min", "coredumpctl --no-pager list -n 5",
    ):
        _ok(cmd)


def test_quotes_and_globs():
    _ok("cat /etc/pve/nodes/*/qemu-server/*.conf")
    _ok('cat "/var/log/path with space.log"')
    _ok("grep -E '^(a|b);$x`y`\\z' f")          # single quotes: all literal
    _ok('journalctl -g "reboot|shutdown" --no-pager')
    _ok("pct exec 216 -- ss -tn state established '( sport = :8080 )'")
    _no('grep "$HOME" f')                         # double quotes still expand $ ` \
    _no('grep "`id`" f')
    _no("grep 'x f")                              # unterminated
    _no("ls *")                                   # a leading glob can expand to `-rf`
    _no("ls ''*")
    _no("grep -F [ f")
    _no("cat f #x")                               # a comment
    _no("cat f\tx\x0b")                           # control character


def test_rebuilt_command_is_what_runs():
    err, run = read_allow.check("grep  -E 'a|b'   /etc/pve/*.conf")
    assert err is None and run == "grep -E 'a|b' /etc/pve/*.conf", run
    err, run = read_allow.check("journalctl --since='1 hour ago' -u x")
    assert err is None and run == "journalctl --since='1 hour ago' -u x", run
    calls = []

    async def spy(cmd, timeout=60.0):
        del timeout
        calls.append(cmd)
        return 0, "ok", ""

    from proxmox_mcp import config, host_ssh
    from proxmox_mcp.tools.host_read import HostReadInput, proxmox_host_read_exec
    saved = host_ssh.exec_command, host_ssh.audit_log, config.require_ssh
    host_ssh.exec_command, host_ssh.audit_log = spy, lambda *_, **__: None
    config.require_ssh = lambda: None
    try:
        out = asyncio.run(proxmox_host_read_exec(HostReadInput(command="cat  'a b'")))
        refused = asyncio.run(proxmox_host_read_exec(HostReadInput(command="cat a; id")))
    finally:
        host_ssh.exec_command, host_ssh.audit_log, config.require_ssh = saved
    assert "rc=0" in out and calls == ["cat 'a b'"], (out, calls)
    assert refused.startswith("Refused") and calls == ["cat 'a b'"], refused


def test_rejects_shell_operators():
    for cmd in (
        "cat /etc/hosts; rm -rf /", "cat /etc/hosts && reboot", "cat /etc/hosts | tee /tmp/x",
        "cat /etc/hosts > /tmp/out", "cat /etc/hosts >> /tmp/out", "echo $(rm -rf /)",
        "cat `whoami`", "cat ${HOME}/x", "(rm -rf /tmp)", "rm {a,b}", "cat a\nrm b",
        "cat a & disown", "cat a | grep b", "cat <<EOF\nx\nEOF", "cat <(id)", "! cat a",
        "cat a\\ b", "X=1 cat a",
    ):
        _no(cmd)


def test_rejects_unknown_programs_and_subcommands():
    for cmd in (
        "rm -rf /", "echo hello", "bash -c 'rm -rf /'", "sh", "dd if=/dev/zero of=/dev/sda",
        "wipefs -a /dev/sdb", "nano /etc/fstab", "/tmp/x/cat /etc/hosts", "./cat x",
        # dropped in madde 309: no real use, and they have write/state options
        "tree /etc", "lshw -short", "sensors", "zdb -C nvmepool", "nvme list",
        "lvdisplay /dev/pve/root", "lsscsi",
        "systemctl restart pvedaemon", "systemctl stop corosync", "systemctl disable pve-ha-lrm",
        "zpool destroy nvmepool", "zpool scrub nvmepool", "zfs destroy nvmepool/subvol-205-disk-0",
        "zfs set compression=off nvmepool", "pct stop 205", "qm stop 101", "pvesm remove local",
        "apt install htop", "apt-mark hold pve-manager", "chronyc makestep",
        "proxmox-boot-tool init /dev/sdd2", "coredumpctl dump 1234", "coredumpctl debug",
        "pvesh set /nodes/x", "pvesh create /x", "apt-cache gencaches", "zpool",
        "systemctl -- stop x", "systemctl --no-pager -- restart x", "zfs -- destroy p/x",
    ):
        _no(cmd)


def test_rejects_unlisted_options_and_abbreviations():
    for cmd in (
        "tail -f /var/log/syslog", "tail --follow /var/log/syslog", "tail -F x", "journalctl -f",
        "journalctl --rotate", "journalctl --vacuum-time=1s", "journalctl --flush",
        "journalctl --no-pa", "smartctl -t short /dev/sda", "smartctl --test=long /dev/sda",
        "date --se 2020-01-01", "ls --al", "grep --colo x f", "df --sync",
        "free -s 1", "ls --all=x", "grep -efoo f", "zpool status -c smart",
        "zpool iostat -c util", "ip -n x link", "ip netns exec x id", "lspci -xxx",
        "ps k -pcpu", "journalctl --image=/x.raw", "file -z f", "lspci -q",
    ):
        _no(cmd)


# madde 312 (deny-list era): every form that changed state through a read program
MADDE_312 = (
    "ip link set vmbr0 down", "ip -s link set vmbr0 down", "ip addr add 10.0.0.9/24 dev vmbr0",
    "ip route flush all", "ip neigh flush all", "date -s '2020-01-01'",
    "date --set=2020-01-01", "hostname evil", "hostname -F /etc/passwd",
    "tree -o /etc/passwd /", "ss -K dst 192.168.1.1", "arcstat -o /etc/passwd 1 1",
    "smartctl -s off /dev/sda", "smartctl --smart=off /dev/sda",
    "nvme telemetry-log /dev/nvme0 --output-file=/etc/passwd",
    "journalctl --setup-keys", "journalctl --update-catalog", "sensors -s",
    "lshw -dump /etc/passwd", "file -C -m /etc/passwd", "apt-cache gencaches",
    "pct exec 200 -- ip link set eth0 down", "zdb -e -F nvmepool",
    "ip link s vmbr0 down", "ip -b /tmp/cmds", "ip -force -batch /tmp/x", "ip netns exec x id",
    "ip netns add x", "ip rule del pref 100", "ip address del 1.2.3.4/24 dev x",
    "date 010100002020", "date --se 2020-01-01", "hostname -b x", "tree -R -H . /",
    "ss --kill", "ss -D /tmp/x", "smartctl -o on /dev/sda", "smartctl -S on /dev/sda",
    "smartctl -X /dev/sda", "smartctl -l scterc,70,70 /dev/sda", "smartctl --set=standby,now /dev/sda",
    "journalctl --cursor-file=/tmp/c", "journalctl --relinquish-var", "journalctl --sync",
    "journalctl -n --setup-keys", "file -p /etc/hosts", "apt-cache -p /etc/passwd show x",
    "apt-cache -o Dir::Cache=/tmp show x", "zdb -U /etc/zfs/x nvmepool", "zdb -x /tmp nvmepool",
    "systemctl --state status stop pveproxy", "systemctl -H status stop x",
    "coredumpctl -o /etc/passwd info 1", "nvme get-log /dev/nvme0 -O /tmp/x",
    "lshw -short -dump /tmp/x", "dmesg -C", "dmesg --clear", "dmesg -n 1",
    "dmesg --console-level=1", "dmesg -w", "dmidecode --dump-bin /tmp/smbios.bin",
)
# madde 316: getopt prefixes, `-oKEY=VAL`, an unlisted smartctl log, a heredoc to bash
MADDE_316 = (
    "dmidecode --dump-b /tmp/x", "dmidecode --dump-bi=/tmp/x", "dmesg --cle", "dmesg --read-c",
    "nvme get-log /dev/nvme0 --output-fi=/tmp/x", "coredumpctl --outp=/tmp/x info 1",
    "coredumpctl info --outp /tmp/x 1", "apt-cache -oDir::Cache=/tmp show x",
    "apt-cache -oDir::Cache::pkgcache=/tmp/x policy x", "apt -oDir::Cache=/tmp list",
    "smartctl -l scttempint,10 /dev/sda", "smartctl -l scttempint,10,p /dev/sda",
    "smartctl --log=scttempint,10 /dev/sda", "smartctl -lscttempint,10 /dev/sda", "bash /dev/stdin <<'EOF'\neval x\nEOF",
    "pct exec 200 -- bash /dev/stdin",
)
# madde 317 (sys second layer): these were S1 there
MADDE_317 = (
    "pct exec 200 reboot", "pct exec 200 -- pct exec 201 -- reboot",
    "pct exec 200 -- pct exec 200 -- cat /etc/hosts", "pct destroy 200", "qm stop 101",
    "zfs destroy nvmepool/x", "zpool destroy nvmepool", "systemctl stop pve-cluster",
    "pct exec 200 --keep-env -- cat x", "pct exec 200 -- systemctl stop pve-cluster",
)


def test_regression_findings_all_refused():
    import test_safety_gates as g   # tests/ is on sys.path
    found = (g.MADDE_295 + g.MADDE_310_REGRESSIONS + g.MADDE_310_WRAPPERS
             + g.MADDE_310_HEREDOCS + g.MADDE_310_UNMODELLED
             + ("$'\\x65val' x", "$'\\145val' x", "$'\\x72m' -rf /x", '$"eval" x')
             + MADDE_312 + MADDE_316 + MADDE_317)
    assert len(found) > 150, len(found)
    for cmd in found:
        _no(cmd)


def test_normal_reads_still_pass():
    # madde 312/309 false-positive check: everyday read forms
    for cmd in (
        "ip -br a", "ip -br link", "ip a", "ip addr show vmbr0", "ip -4 -o addr show",
        "ip route", "ip route show table all", "ip route get 1.1.1.1", "ip -s link show eno1",
        "ip neigh", "ip -j -p addr", "ip rule list", "ip -c=never a",
        "ss -tlnp", "ss -tunap", "ss -s", "ss -lntp sport = :22", "ss --tcp --listening --numeric",
        "journalctl -u pveproxy -n 50 --no-pager", "journalctl -b -1 -p err --no-pager",
        "journalctl -n50 -o cat -u x", "journalctl --since '1 hour ago' --until now -u x",
        "journalctl -k -b", "journalctl --disk-usage", "journalctl --list-boots",
        "journalctl -u x --since=today -g error", "journalctl -t CRON -r -n 20",
        "journalctl -S -20min -U -5min", "journalctl --boot -1 -M 207 -u x",
        "smartctl -a /dev/sda", "smartctl -x /dev/nvme0", "smartctl -H /dev/sdb",
        "smartctl -i -d sat /dev/sda", "smartctl -l selftest /dev/sda", "smartctl -lselftest /dev/sda", "smartctl -l xerror,8 /dev/sda",
        "smartctl --scan", "smartctl -j -a /dev/sda", "smartctl --all --json=c /dev/sda",
        "date", "date -u", "date +%s", "date -I", "date -Iseconds", "date -d @0", "date -R",
        "date -d '1 day ago' +%F", "hostname", "hostname -f", "hostname -I", "hostname --fqdn",
        "arcstat 1 3", "arcstat -f time,hit% 1 1", "arc_summary -s arc",
        "file /etc/hosts", "file -L -b /usr/bin/python3", "apt-cache show zfsutils-linux",
        "apt-cache search --names-only zfs", "systemctl --no-pager status pveproxy",
        "systemctl list-units --type=service --state=failed", "coredumpctl --no-pager list",
        "pct exec 200 -- ip -br a", "pct exec 209 -- journalctl -u homelab-agent-ha -n 50 --no-pager",
        "pct exec 200 -- systemctl status postgresql", "pct exec 201 -- df -h",
        "pct exec 204 -- cat /etc/hosts", "pct exec 207 -- grep -E 'a|eval' f",
    ):
        _ok(cmd)


def test_pct_exec_form():
    for cmd in (
        "pct exec 200 systemctl status postgresql",   # no '--'
        "pct exec -- df -h", "pct exec abc -- df -h", "pct exec 200 --",
        "pct exec 200 -- rm -rf /", "pct exec 200 -- systemctl restart postgresql",
        "pct exec 200 -- dmesg --clear", "pct exec 200 -- tail -f /var/log/syslog",
        "pct exec 200 -- bash -c 'id'", "pct exec 200 -- cat /etc/shadow; id",
        "pct exec 200 -- sh -c \"cat /etc/passwd | wc -l\"",
    ):
        _no(cmd)


def test_rejects_empty_and_non_strings():
    _no("")
    _no("   ")
    assert read_allow.validate(None) is not None
    assert read_allow.validate(["ls"]) is not None


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
