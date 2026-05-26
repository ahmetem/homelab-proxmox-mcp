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
    # Cluster / nodes
    "proxmox_list_nodes",
    "proxmox_get_node_status",
    # VMs / containers
    "proxmox_list_vms",
    "proxmox_get_vm_status",
    "proxmox_vm_start",
    "proxmox_vm_shutdown",
    "proxmox_vm_stop",
    "proxmox_vm_reboot",
    "proxmox_resize_vm",
    # Snapshots
    "proxmox_list_snapshots",
    "proxmox_create_snapshot",
    "proxmox_rollback_snapshot",
    "proxmox_delete_snapshot",
    # Backups
    "proxmox_list_backups",
    "proxmox_create_backup",
    # Storage (pool listing)
    "proxmox_list_storage",
    # Phase 1: disks / LVM / ZFS inventory
    "proxmox_list_disks",
    "proxmox_get_disk_smart",
    "proxmox_list_lvm",
    "proxmox_list_lvm_thin",
    "proxmox_list_zfs",
    "proxmox_get_zfs_pool",
    # Phase 2: disk preparation
    "proxmox_disk_init_gpt",
    "proxmox_wipe_disk",
    # Phase 2: LVM create / destroy
    "proxmox_create_lvm_vg",
    "proxmox_create_lvm_thin",
    "proxmox_destroy_lvm_vg",
    "proxmox_destroy_lvm_thin",
    # Phase 2: ZFS create / destroy
    "proxmox_create_zfs_pool",
    "proxmox_destroy_zfs_pool",
    # Phase 2: cluster storage management
    "proxmox_list_cluster_storage",
    "proxmox_add_zfs_storage",
    "proxmox_add_dir_storage",
    "proxmox_remove_storage",
    # Phase 2.5: SSH-backed (bypass token restrictions, dataset/property/snapshot ops)
    "proxmox_ssh_wipe_disk",
    "proxmox_ssh_init_gpt",
    "proxmox_zfs_create_dataset",
    "proxmox_zfs_destroy_dataset",
    "proxmox_zfs_set_property",
    "proxmox_zfs_create_snapshot",
    "proxmox_zfs_list_datasets",
    # Phase 3: VM disk movement / clone / ISO listing
    "proxmox_move_disk",
    "proxmox_clone_vm",
    "proxmox_list_isos",
    # Phase 3: ZFS property read / pool status / scrub / replication
    "proxmox_zfs_get_property",
    "proxmox_zfs_pool_status",
    "proxmox_zfs_scrub",
    "proxmox_zfs_send",
    # Phase 4: guest VM SSH (full shell exec, audit-logged)
    "proxmox_vm_list_hosts",
    "proxmox_vm_exec",
    "proxmox_vm_read_file",
    # Phase 5: Proxmox host SSH (full shell exec, audit-logged)
    "proxmox_host_exec",
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
