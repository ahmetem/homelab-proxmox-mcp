# Proxmox MCP Server

A local [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server
that lets Claude Desktop (or any MCP-compatible client) manage a
**Proxmox VE** cluster through its REST API using token authentication.

Tested with **Proxmox VE 9.1.9** on a single-node setup. Multi-node clusters
should work — every tool accepts a `node` parameter.

> 🇹🇷 Türkçe README için: [README.tr.md](./README.tr.md)

## Features

The package exposes **63 tools** across nine phases (0–7, plus an extra
"2.5"), grouped by category below. Read-only tools are safe to call
automatically; every state-changing tool requires `confirm=true`, and
tools that destroy persistent data additionally require
`i_understand_data_loss=true` (see **Built-in safety**).

### Cluster & nodes (read-only)

| Tool | Description |
|---|---|
| `proxmox_list_nodes` | List all nodes in the cluster with status, uptime, CPU and memory usage |
| `proxmox_get_node_status` | Detailed status for one node: CPU model, kernel, load average, disk, swap |

### VMs & containers

| Tool | Description |
|---|---|
| `proxmox_list_vms` | List every VM and LXC container across the cluster (read-only) |
| `proxmox_get_vm_status` | Detailed runtime metrics for a single VM/CT (read-only) |
| `proxmox_vm_start` | Start a VM or LXC container |
| `proxmox_vm_shutdown` | Graceful ACPI shutdown |
| `proxmox_vm_stop` | Force stop (pull the plug) — may cause data loss |
| `proxmox_vm_reboot` | Graceful reboot, then power cycle if needed |
| `proxmox_resize_vm` | Change RAM (`memory_mb`) and/or CPU `cores` of a VM/CT |

### Snapshots

| Tool | Description |
|---|---|
| `proxmox_list_snapshots` | Snapshots for a specific VM/CT (read-only) |
| `proxmox_create_snapshot` | Create a snapshot of a VM/CT |
| `proxmox_rollback_snapshot` | Rollback to a snapshot — data after it is lost |
| `proxmox_delete_snapshot` | Delete a named snapshot of a VM/CT |

### Backups

| Tool | Description |
|---|---|
| `proxmox_list_backups` | Backup files on a storage (read-only) |
| `proxmox_create_backup` | Create a backup with selectable mode and compression |
| `proxmox_restore_backup` | Restore a VM/CT from a backup archive. Refuses to overwrite an existing VMID unless `force=true` and `i_understand_data_loss=true`. |

### Storage (read-only)

| Tool | Description |
|---|---|
| `proxmox_list_storage` | Storage pools on a node with usage info |
| `proxmox_storage_usage_detail` | Per-storage content breakdown: items by type with totals, plus top-N largest items. Useful for capacity planning ("which VM is eating my backup storage?"). |

### Phase 1 — disk / LVM / ZFS inventory (read-only)

| Tool | Description |
|---|---|
| `proxmox_list_disks` | Physical disks on a node: model, size, type, health, usage |
| `proxmox_get_disk_smart` | SMART health and attributes for one physical disk |
| `proxmox_list_lvm` | LVM volume groups and logical volumes on a node |
| `proxmox_list_lvm_thin` | LVM-thin pools on a node |
| `proxmox_list_zfs` | ZFS pools on a node with capacity/health |
| `proxmox_get_zfs_pool` | Detailed status of a single ZFS pool |

### Phase 2 — disk preparation & storage provisioning

| Tool | Description |
|---|---|
| `proxmox_disk_init_gpt` | Initialize a blank disk with a GPT partition table |
| `proxmox_wipe_disk` | Wipe all partitions/signatures from a disk (destructive) |
| `proxmox_create_lvm_vg` | Create an LVM volume group on a disk |
| `proxmox_create_lvm_thin` | Create an LVM-thin pool |
| `proxmox_destroy_lvm_vg` | Destroy an LVM volume group (destructive) |
| `proxmox_destroy_lvm_thin` | Destroy an LVM-thin pool (destructive) |
| `proxmox_create_zfs_pool` | Create a ZFS pool across one or more disks |
| `proxmox_destroy_zfs_pool` | Destroy a ZFS pool (destructive) |
| `proxmox_list_cluster_storage` | List datacenter-level storage configuration (read-only) |
| `proxmox_add_zfs_storage` | Register an existing ZFS pool as Proxmox storage |
| `proxmox_add_dir_storage` | Register a directory as Proxmox storage |
| `proxmox_remove_storage` | Remove a storage entry from the datacenter config |

### Phase 2.5 — SSH-backed ZFS / disk ops

These run shell commands on the node over SSH, for operations the API
token can't perform. They need the SSH configuration described in the
[Configuration reference](#configuration-reference).

| Tool | Description |
|---|---|
| `proxmox_ssh_wipe_disk` | Wipe a disk over SSH (destructive) |
| `proxmox_ssh_init_gpt` | Initialize a GPT label over SSH |
| `proxmox_zfs_create_dataset` | Create a ZFS dataset/filesystem |
| `proxmox_zfs_destroy_dataset` | Destroy a ZFS dataset (destructive) |
| `proxmox_zfs_set_property` | Set a property on a ZFS dataset |
| `proxmox_zfs_create_snapshot` | Create a ZFS snapshot of a dataset |
| `proxmox_zfs_list_datasets` | List ZFS datasets (read-only) |
| `proxmox_zfs_destroy_snapshots_by_pattern` | Bulk-delete ZFS snapshots whose name matches a glob pattern. Two-step: `dry_run=true` (default) lists matches; `dry_run=false` with `confirm=true` and `i_understand_data_loss=true` actually deletes. Capped at `max_delete` (default 1000). |

### Phase 3 — VM disk / clone / ISO + ZFS status

| Tool | Description |
|---|---|
| `proxmox_move_disk` | Move a VM disk to another storage |
| `proxmox_clone_vm` | Clone a VM/CT to a new VMID |
| `proxmox_list_isos` | List ISO images available on storages (read-only) |
| `proxmox_zfs_get_property` | Read a property from a ZFS dataset/pool (read-only) |
| `proxmox_zfs_pool_status` | `zpool status` for a pool: health, errors, scrub state (read-only) |
| `proxmox_zfs_scrub` | Start a scrub on a ZFS pool |
| `proxmox_zfs_send` | ZFS send stream for replication |

### Phase 4 — guest VM SSH (full shell, audit-logged)

| Tool | Description |
|---|---|
| `proxmox_vm_list_hosts` | List guest VM aliases registered in `vm_ssh_hosts.json` (read-only) |
| `proxmox_vm_exec` | Run a shell command on a registered guest VM via SSH (uses `vm_ssh_hosts.json`). Requires `confirm=true`. |
| `proxmox_vm_read_file` | Read a file from a registered guest VM over SSH (read-only) |

### Phase 5 — Proxmox host SSH (full shell, audit-logged)

| Tool | Description |
|---|---|
| `proxmox_host_exec` | Run an arbitrary shell command on the Proxmox host over SSH. Requires `confirm=true`; commands matching destructive patterns also require `i_understand_data_loss=true`. Every call is appended to `_host_ssh_audit.log`. |

### Phase 6 — LXC exec

| Tool | Description |
|---|---|
| `proxmox_lxc_exec` | Run a shell command inside an LXC container via `pct exec` from the Proxmox host. No SSH inside the CT needed. Requires `confirm=true`. |
| `proxmox_ct_service_action` | Typed shortcut on top of `proxmox_lxc_exec`: `systemctl --no-pager <action> <service>` inside a CT. Read-only actions (status, is-active, is-enabled, show) skip `confirm`; state-changing actions (start, stop, restart, reload, enable, disable, mask, unmask) require `confirm=true`. |
| `proxmox_ct_log_tail` | Read-only tail of a journald unit or log file from inside a CT. Two modes: `service` (journalctl -u) and `file` (tail -n). Optional `grep` server-side filter. |

### Phase 7 — diagnostics, forensics & cleanup

| Tool | Description |
|------|-------------|
| `proxmox_zfs_list_snapshots` | List ZFS snapshots (name, used, referenced, creation) directly via SSH, **including** transient `@vzdump` scratch snapshots the PVE config-snapshot API never reports. Filter by `dataset` and/or `contains`. (read-only) |
| `proxmox_list_tasks` | List recent PVE tasks (vzdump/snapshot/...) with start time, status and UPID. Filter by `typefilter`, `vmid`, `errors_only`. (read-only) |
| `proxmox_get_task_log` | Read a PVE task's log lines by UPID — e.g. the exact reason a `vzdump` run failed. (read-only) |
| `proxmox_list_backup_jobs` | List configured `vzdump` jobs (schedule, storage, vmids, retention) from `/cluster/backup`. (read-only) |
| `proxmox_cleanup_vzdump_snapshots` | **Self-heal.** Remove leftover `@vzdump` ZFS snapshots that block a guest's backups (a stale scratch snapshot from an interrupted run makes every later backup fail with `dataset already exists`). Targets **only** names ending in `@vzdump` and skips any younger than `min_age_minutes` (default 30) so it can never race a running backup. `dry_run=true` by default; real removal needs `dry_run=false` + `confirm=true`. |

### Built-in safety

All destructive or state-changing actions require `confirm=true`. Tools
that destroy persistent data (snapshot delete, backup restore with
overwrite, disk wipe, LVM/ZFS pool/dataset destroy, bulk snapshot
destroy, etc.) additionally require `i_understand_data_loss=true`. The
agent must explicitly pass both flags, which in practice means Claude
only fires these after the user clearly asks for the action. Read-only
tools have no such guard.

## Requirements

- **Python 3.11+**
- A Proxmox VE host you can reach over HTTPS (default port 8006)
- A Proxmox **API token** with the right privileges (see below)
- Claude Desktop (or any MCP client)

## 1. Create a Proxmox API token

The server authenticates with an API token, never with a root password. Tokens
can be revoked individually and limit blast radius.

1. Log in to the Proxmox web UI.
2. Go to **Datacenter → Permissions → API Tokens**.
3. Click **Add**:
   - **User**: `root@pam` (or a dedicated user — recommended)
   - **Token ID**: `mcp-server` (any name)
   - **Privilege Separation**: keep enabled unless you know you want otherwise
4. Click **Add**. A dialog shows the **secret** value (a UUID) **once**.
   Copy it now; you cannot retrieve it again.

If you keep Privilege Separation enabled, you must also grant the token
permissions. Go to **Datacenter → Permissions → Add → API Token Permission**:

- **Path**: `/` (or narrower if you prefer)
- **API Token**: the token you just made
- **Role**: `PVEAdmin` for full access, or `PVEVMAdmin` if you only want
  VM/CT management
- **Propagate**: checked

You can scope this much more tightly in production. For a homelab `/` with
`PVEAdmin` is the simplest.

## 2. Install the server

### Windows (PowerShell)

```powershell
git clone https://github.com/<your-username>/proxmox-mcp.git C:\mcp-servers\proxmox-mcp
cd C:\mcp-servers\proxmox-mcp

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell blocks the activation script, run this once in an
administrator PowerShell:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

### Linux / macOS

```bash
git clone https://github.com/<your-username>/proxmox-mcp.git ~/mcp-servers/proxmox-mcp
cd ~/mcp-servers/proxmox-mcp

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Configure `.env`

```powershell
copy .env.example .env
notepad .env
```

Fill in:

```ini
PROXMOX_HOST=192.168.1.10            # IP or hostname of your Proxmox host
PROXMOX_PORT=8006                    # default
PROXMOX_USER=root@pam                # the user owning the token
PROXMOX_TOKEN_NAME=mcp-server        # the Token ID you chose
PROXMOX_TOKEN_VALUE=xxxxxxxx-xxxx-...  # the secret UUID
PROXMOX_VERIFY_SSL=false             # most homelabs use self-signed certs
PROXMOX_TIMEOUT=30
```

**Never** commit `.env` to git. The included `.gitignore` already excludes it,
but double-check.

## 4. Smoke test

With the venv active:

```powershell
python proxmox_mcp.py --help
```

You should see the tool list and exit cleanly. An import error here means a
dependency didn't install correctly.

## 5. Register with Claude Desktop

Open Claude Desktop's config file:

- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

If the file doesn't exist, create it. Add (or extend) the `mcpServers` block:

```json
{
  "mcpServers": {
    "proxmox": {
      "command": "C:\\mcp-servers\\proxmox-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\mcp-servers\\proxmox-mcp\\proxmox_mcp.py"],
      "cwd": "C:\\mcp-servers\\proxmox-mcp"
    }
  }
}
```

Adjust paths for your OS. On Windows, double-backslashes are required inside
JSON strings.

Fully quit Claude Desktop (tray icon → Quit) and reopen it. In a new chat the
Proxmox tools appear under the hammer/connector icon.

## First test in chat

Start with a read-only call:

> "List my Proxmox nodes."

Claude calls `proxmox_list_nodes`. You should see a list of nodes with their
status. If you get an authentication error, recheck `PROXMOX_TOKEN_VALUE` and
the token's permissions.

Then try:

> "Show me all VMs."
>
> "What's the status of VM 101?"
>
> "List backups on the local storage of node pve."

Once you're confident the read-only tools work, you can try action tools:

> "Reboot VM 101."

Claude will ask you to confirm. After you say yes, it calls
`proxmox_vm_reboot` with `confirm=true`.

## Example workflows

**Resize a VM and reboot it:**

> "Set VM 101 to 4 GB of RAM, then reboot it."

Claude calls `proxmox_resize_vm` with `memory_mb=4096, confirm=true`, then
`proxmox_vm_reboot` with `confirm=true`.

**Quick backup before a risky upgrade:**

> "Create a snapshot of VM 102 called `pre-upgrade` with description
> 'before kernel update'."

Claude calls `proxmox_create_snapshot` with `confirm=true`.

**Inspect health:**

> "Are any storages above 80% full?"

Claude calls `proxmox_list_storage` and summarizes.

## Configuration reference

All settings come from environment variables, loaded from `.env`:

| Variable | Default | Description |
|---|---|---|
| `PROXMOX_HOST` | — (required) | IP or hostname of the Proxmox host |
| `PROXMOX_PORT` | `8006` | API port |
| `PROXMOX_USER` | — (required) | User owning the token (e.g. `root@pam`) |
| `PROXMOX_TOKEN_NAME` | — (required) | API token ID |
| `PROXMOX_TOKEN_VALUE` | — (required) | API token secret (UUID) |
| `PROXMOX_VERIFY_SSL` | `false` | Verify the TLS certificate of the API |
| `PROXMOX_TIMEOUT` | `30` | HTTP timeout in seconds |

### SSH (optional — required by Phase 2.5 / 4 / 5 / 6 tools)

The SSH-backed tools run shell commands on the Proxmox host (and, for
`proxmox_vm_*`, on guest VMs). They are inactive until you configure SSH.
`PROXMOX_SSH_HOST` defaults to `PROXMOX_HOST`. Prefer key auth; if both a
key and a password are set, the key is used. Guest VMs are defined
separately in `vm_ssh_hosts.json`.

| Variable | Default | Description |
|---|---|---|
| `PROXMOX_SSH_HOST` | = `PROXMOX_HOST` | Host for the host-level SSH tools |
| `PROXMOX_SSH_PORT` | `22` | SSH port |
| `PROXMOX_SSH_USER` | `root` | SSH user |
| `PROXMOX_SSH_KEY_PATH` | — | Path to a private key (preferred auth) |
| `PROXMOX_SSH_PASSWORD` | — | Password auth (fallback if no key) |
| `PROXMOX_SSH_KNOWN_HOSTS` | — | Path to a known_hosts file; `ignore` skips host-key verification (trusted LAN only) |
| `PROXMOX_SSH_TIMEOUT` | `30` | SSH connect timeout in seconds |

## Security notes

- The token secret sits in `.env`. Restrict that file to your user account
  (`icacls` on Windows; `chmod 600` on Linux).
- Never expose the Proxmox API to the internet. Keep it on a trusted
  LAN/VLAN, or behind a VPN.
- Privilege-separate the API token. Use a non-root user where possible.
- Action tools require `confirm=true`. Don't remove that guard.
- `PROXMOX_VERIFY_SSL=false` is the default because homelab certs are
  usually self-signed. If you've installed a trusted certificate, set it to
  `true`.

## Troubleshooting

- **"Authentication failed. Check PROXMOX_TOKEN_VALUE."**
  The token secret is wrong, or the token user is wrong. The user must match
  the user the token was created under (e.g. `root@pam`, not just `root`).

- **"Permission denied. Token lacks privileges."**
  You probably have privilege separation enabled on the token but haven't
  given the token a role yet. Go to **Datacenter → Permissions** and add an
  **API Token Permission** for it.

- **"Cannot connect to <host>:8006"**
  Network problem. Ping the host. Check the firewall on both ends. Confirm
  the web UI loads at `https://<host>:8006/` from the same machine.

- **"Request timed out after 30s"**
  Increase `PROXMOX_TIMEOUT` or check that the Proxmox node isn't under
  heavy load.

- **Tools don't appear in Claude Desktop.**
  Check `%APPDATA%\Claude\logs\mcp*.log` (Windows) or
  `~/Library/Logs/Claude/mcp*.log` (macOS) for errors. The most common cause
  is a wrong path in `claude_desktop_config.json` or backslashes that
  weren't doubled.

## Project structure

```
proxmox-mcp/
├── proxmox_mcp/             # The MCP server package
│   ├── __main__.py          # entry for `python -m proxmox_mcp`
│   ├── server.py            # entry point + full tool registry (TOOLS)
│   ├── mcp_instance.py      # the shared FastMCP instance
│   ├── config.py            # environment-variable configuration
│   ├── http_client.py       # Proxmox REST client
│   ├── ssh.py / host_ssh.py / vm_ssh.py   # SSH backends (host + guests)
│   └── tools/               # one module per tool group (nodes, vms,
│                            #   disks, lvm, zfs, backups, ct_ops, …)
├── proxmox_mcp.py           # compatibility shim (`python proxmox_mcp.py`)
├── requirements.txt         # Python dependencies
├── .env.example             # Template for your local .env
├── vm_ssh_hosts.json        # (git-ignored) guest-VM SSH registry
├── .gitignore
├── LICENSE                  # GPL v3
├── README.md                # This file
└── README.tr.md             # Turkish version
```

## Contributing

Issues and PRs welcome. If you add a tool, please:

1. Follow the existing pattern: pydantic input model + `_require_config` +
   error handling.
2. Tag destructive tools with `destructiveHint: True` in the annotations and
   require `confirm=True` in the input model.
3. Update the tool list in this README.

## License

[GNU General Public License v3.0](./LICENSE) — see the `LICENSE` file for the
full text.
