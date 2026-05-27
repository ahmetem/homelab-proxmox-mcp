"""Tool modules. Importing this package registers all tools with the FastMCP instance."""

from proxmox_mcp.tools import (  # noqa: F401
    # Phase 0 (original)
    nodes,
    vms,
    storage,
    snapshots,
    backups,
    # Phase 1 (read-only inventory)
    disks,
    lvm,
    zfs,
    # Phase 2 (disk preparation + pool create/destroy + cluster storage)
    disks_prepare,
    lvm_manage,
    zfs_manage,
    storage_manage,
    # Phase 2.5 (SSH-backed: bypass API token restrictions + dataset/snapshot ops)
    ssh_disks,
    ssh_zfs,
    # Phase 3 (VM disk movement / clone / ISO listing + ZFS get/status/scrub/send)
    vm_disk,
    ssh_zfs_phase3,
    # Phase 4 (guest VM SSH — full shell exec, audit-logged)
    vm_ssh,
    # Phase 5 (Proxmox host SSH — full shell exec, audit-logged)
    host_ssh,
    # Phase 6 (LXC container exec via pct exec — typed wrapper on host SSH)
    lxc_exec,
    ct_ops,
)
