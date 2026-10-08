"""Guard: never raw-`zfs destroy` a snapshot that PVE has registered.

A guest snapshot taken with `pct/qm snapshot` lives in two places: the ZFS
snapshot and a `[<snapname>]` section in /etc/pve/{lxc,qemu-server}/<vmid>.conf.
Destroying only the ZFS half leaves the config section behind; a later
`pct/qm delsnapshot` then fails mid-way and leaves the guest in
`lock: snapshot-delete`, which blocks start. That is how CT 200 (Postgres) and
CT 202 stayed down after the 2026-10-08 reboot (raw `zfs destroy` loop on
2026-10-05; incident: homelab-project/06-known-issues.md).

Every ZFS-destroy path in this server consults `registered_hits()` and refuses
with a pointer to `pct/qm delsnapshot`. There is no override flag: the incident
command already carried i_understand_data_loss=true. Snapshots PVE does not
know about (sanoid `autosnap_*`, ad-hoc `zfs snapshot`) pass, because they have
no config section — no name is special-cased.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from proxmox_mcp import host_ssh

ZFS_DESTROY_RE = re.compile(r"\bzfs\s+destroy\b", re.IGNORECASE)

# Last path segment of a guest disk dataset: subvol-200-disk-0, vm-101-disk-1, base-9000-disk-0
_GUEST_DS_RE = re.compile(r"(?:^|/)(?:subvol|vm|base)-(\d+)-disk-\d+$")
# One line of the READ_CMD output: /etc/pve/lxc/200.conf:[daily_20261008]
_CONF_HEADER_RE = re.compile(r"/(\d+)\.conf:\[([^\]]+)\]\s*$")
# `<dataset>@<snap>[,<snap>...]` or `<dataset>@<a>%<b>` anywhere in a shell command.
# The dataset part may be a shell variable ($ds, ${P}/subvol-...) or empty.
_SNAP_REF_RE = re.compile(r"([A-Za-z0-9_.:/${}-]*)@([A-Za-z0-9_.:%,-]+)")

# Section headers of every guest config. `test -e .version` fails when pmxcfs
# (/etc/pve) is not mounted, so an empty answer can't be mistaken for "no snapshots".
READ_CMD = (
    "test -e /etc/pve/.version && "
    "{ grep -H '^\\[' /etc/pve/lxc/*.conf /etc/pve/qemu-server/*.conf 2>/dev/null; true; }"
)

# Config sections that are not snapshots.
_NOT_SNAPSHOTS = {"PENDING"}


def vmid_of(dataset: str) -> Optional[int]:
    m = _GUEST_DS_RE.search(dataset)
    return int(m.group(1)) if m else None


def parse_registered(text: str) -> dict[int, set[str]]:
    """READ_CMD output -> {vmid: {snapshot section names}}."""
    out: dict[int, set[str]] = {}
    for line in text.splitlines():
        m = _CONF_HEADER_RE.search(line.strip())
        if not m:
            continue
        name = m.group(2)
        if name in _NOT_SNAPSHOTS or name.startswith("special:"):
            continue
        out.setdefault(int(m.group(1)), set()).add(name)
    return out


async def load_registered() -> dict[int, set[str]]:
    """Read every guest config's snapshot sections. Raises RuntimeError if
    /etc/pve can't be read — callers must refuse (fail closed) in that case."""
    try:
        rc, out, err = await host_ssh.exec_command(READ_CMD, timeout=30)
    except Exception as exc:
        raise RuntimeError(host_ssh.format_host_ssh_error(exc)) from exc
    if rc != 0:
        raise RuntimeError(
            f"cannot read /etc/pve guest configs (rc={rc}; pmxcfs not mounted?) "
            + (err or out)[:200]
        )
    return parse_registered(out)


def snapshot_refs(command: str) -> list[tuple[str, str]]:
    """Every literal `<dataset>@<snap>` in a shell command, comma lists expanded.

    Scans the WHOLE command, not just `zfs destroy`'s argument, so the incident
    form `for s in subvol-208-disk-0@a,b ...; do zfs destroy $P/$s; done` is
    still seen. `%` ranges are kept whole (see registered_hits).
    """
    refs: list[tuple[str, str]] = []
    for m in _SNAP_REF_RE.finditer(command):
        ds, snaps = m.group(1), m.group(2)
        for snap in snaps.split(","):
            snap = snap.strip(".")
            if snap:
                refs.append((ds, snap))
    return refs


def registered_hits(
    refs: Iterable[tuple[str, ...]], registered: dict[int, set[str]]
) -> list[tuple[int, str, str]]:
    """(vmid, dataset, snap) for each ref that would destroy a PVE-registered snapshot.

    - Guest dataset (subvol/vm/base-<vmid>-disk-N): checked against that guest.
    - Dataset that is a shell variable, empty, or a pool root (no '/'; reaches
      guests with -r): checked against every guest — the owner can't be known.
    - Any other literal dataset (nvmepool/data): not a guest disk, allowed.
    - `a%b` range: refused if the guest has any registered snapshot, because the
      names in between are not visible here.
    """
    hits: list[tuple[int, str, str]] = []
    for ds, snap in refs:
        vmid = vmid_of(ds)
        if vmid is not None:
            owners = {vmid: registered.get(vmid, set())}
        elif not ds or "$" in ds or "/" not in ds:
            owners = registered
        else:
            continue
        for gid, names in owners.items():
            if ("%" in snap and names) or snap in names:
                hits.append((gid, ds, snap))
    return hits


def refusal(hits: list[tuple[int, str, str]]) -> str:
    lines = [
        "Refused: these snapshots are registered in a PVE guest config. A raw "
        "`zfs destroy` would leave the `[snapshot]` section behind and the next "
        "`pct/qm delsnapshot` leaves the guest locked (snapshot-delete) — the "
        "2026-10-08 CT 200/202 outage. Delete them through PVE instead "
        "(add `--force` if the ZFS half is already gone):",
    ]
    seen = set()
    for vmid, ds, snap in hits:
        if (vmid, snap) in seen:
            continue
        seen.add((vmid, snap))
        tool = "qm" if re.search(r"(?:^|/)vm-\d+-disk-", ds) else "pct"
        lines.append(f"- `{ds}@{snap}` -> `{tool} delsnapshot {vmid} {snap}`")
    return "\n".join(lines)
