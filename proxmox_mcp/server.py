"""Proxmox VE MCP Server entry point.

Run with:
  python -m proxmox_mcp
  python proxmox_mcp.py        (compatibility shim, see top-level proxmox_mcp.py)

Configuration is loaded from environment variables (typically via .env):
    PROXMOX_HOST        - Proxmox host or IP
    PROXMOX_PORT        - API port (default: 8006)
    PROXMOX_USER        - User (e.g. root@pam)
    PROXMOX_TOKEN_NAME  - Token ID
    PROXMOX_TOKEN_VALUE - Token secret UUID
    PROXMOX_VERIFY_SSL  - "true" or "false" (default: false)
    PROXMOX_TIMEOUT     - HTTP timeout seconds (default: 30)
"""
from __future__ import annotations

import sys

from proxmox_mcp.mcp_instance import mcp

# Importing the tools package registers every @mcp.tool decorator with `mcp`.
from proxmox_mcp import tools  # noqa: F401


TOOLS = [
    # Overview
    "proxmox_health_overview",
    # Cluster / nodes
    "proxmox_list_nodes",
    "proxmox_get_node_status",
    # VMs / containers
    "proxmox_list_vms",
    "proxmox_get_vm_status",
    "proxmox_vm_power",
    "proxmox_resize_vm",
    # Snapshots
    "proxmox_list_snapshots",
    "proxmox_snapshot",
    # Backups
    "proxmox_list_backups",
    "proxmox_create_backup",
    "proxmox_restore_backup",
    # Storage (pool listing)
    "proxmox_list_storage",
    "proxmox_storage_usage_detail",
    # Disks / LVM / ZFS inventory
    "proxmox_list_disks",
    "proxmox_get_disk_smart",
    "proxmox_list_lvm",
    "proxmox_list_lvm_thin",
    "proxmox_list_zfs",
    "proxmox_get_zfs_pool",
    # Disk preparation + pool management (disk prep: API with SSH fallback)
    "proxmox_disk_prepare",
    "proxmox_lvm_manage",
    "proxmox_zfs_pool_manage",
    # Cluster storage config
    "proxmox_list_cluster_storage",
    "proxmox_storage_config",
    # SSH-backed ZFS dataset / snapshot ops
    "proxmox_zfs_dataset",
    "proxmox_zfs_create_snapshot",
    "proxmox_zfs_list_datasets",
    "proxmox_zfs_destroy_snapshots_by_pattern",
    # VM disk movement / clone / ISO listing
    "proxmox_move_disk",
    "proxmox_clone_vm",
    "proxmox_list_isos",
    # ZFS property / pool status / scrub / replication
    "proxmox_zfs_property",
    "proxmox_zfs_pool_status",
    "proxmox_zfs_scrub",
    "proxmox_zfs_send",
    # Guest VM SSH (full shell exec, audit-logged)
    "proxmox_vm_list_hosts",
    "proxmox_vm_exec",
    "proxmox_vm_read_file",
    # Proxmox host SSH (full shell exec, audit-logged)
    "proxmox_host_exec",
    # Proxmox host SSH (allow-listed read-only exec)
    "proxmox_host_read_exec",
    # LXC container exec via pct exec (typed wrapper on host SSH)
    "proxmox_lxc_exec",
    "proxmox_ct_service_action",
    "proxmox_ct_log_tail",
    # Read-only forensics + cleanup
    "proxmox_zfs_list_snapshots",
    "proxmox_list_tasks",
    "proxmox_get_task_log",
    "proxmox_list_backup_jobs",
    "proxmox_cleanup_vzdump_snapshots",
]


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in {"-h", "--help"}:
        print(__doc__)
        print("Tools registered:")
        for t in TOOLS:
            print(f"  - {t}")
        sys.exit(0)
    mcp.run()


if __name__ == "__main__":
    main()
