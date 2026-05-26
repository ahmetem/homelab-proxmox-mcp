# Proxmox MCP Server

A local [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server
that lets Claude Desktop (or any MCP-compatible client) manage a **Proxmox VE**
cluster end to end — from listing VMs to creating ZFS pools, replicating
snapshots, and running ad-hoc shell commands on the host or guests.

Tested with **Proxmox VE 9.1.9** on a single-node setup. Multi-node clusters
should work — every tool accepts a `node` parameter.

> 🇦🇷 Türkçe README için: [README.tr.md](./README.tr.md)

## What's in this

**52 tools across five phases**, organized into a small Python package
(`proxmox_mcp/`) with one module per concern. Token-based REST is the default;
SSH is optional and only activated for a handful of operations that need it.

### Phase 0 — cluster, VMs, snapshots, backups (16 tools)

| Tool | Purpose |
|---|---|
| `proxmox_list_nodes` / `proxmox_get_node_status` | Cluster nodes, CPU/memory/uptime |
| `proxmox_list_vms` / `proxmox_get_vm_status` | VM and LXC inventory and detail |
| `proxmox_vm_start` / `vm_shutdown` / `vm_stop` / `vm_reboot` | Power actions |
| `proxmox_resize_vm` | RAM / CPU resize |
| `proxmox_list_snapshots` / `create_snapshot` / `rollback_snapshot` / `delete_snapshot` | Snapshot lifecycle |
| `proxmox_list_backups` / `proxmox_create_backup` | Backups (vzdump) |
| `proxmox_list_storage` | Per-node storage usage |

### Phase 1 — read-only inventory (6 tools)

| Tool | Purpose |
|---|---|
| `proxmox_list_disks` / `proxmox_get_disk_smart` | Block devices + SMART (HDD/SSD/NVMe) |
| `proxmox_list_lvm` / `proxmox_list_lvm_thin` | VGs, PVs, LVs, thin pools |
| `proxmox_list_zfs` / `proxmox_get_zfs_pool` | ZFS pools with vdev tree and error counters |

### Phase 2 — disk prep + pool lifecycle + cluster storage (12 tools)

| Tool | Purpose |
|---|---|
| `proxmox_disk_init_gpt` / `proxmox_wipe_disk` | GPT init / wipefs (REST) |
| `proxmox_create_lvm_vg` / `proxmox_destroy_lvm_vg` | LVM VG lifecycle |
| `proxmox_create_lvm_thin` / `proxmox_destroy_lvm_thin` | LVM-thin pool lifecycle |
| `proxmox_create_zfs_pool` / `proxmox_destroy_zfs_pool` | ZFS pool lifecycle |
| `proxmox_list_cluster_storage` | `/etc/pve/storage.cfg` view |
| `proxmox_add_zfs_storage` / `add_dir_storage` / `remove_storage` | Storage entry management |

### Phase 2.5 — SSH-backed dataset / property / snapshot ops (7 tools)

Proxmox REST refuses API tokens for `wipedisk`/`initgpt`, and doesn't expose
`zfs create`/`destroy`/`set`/`snapshot` for arbitrary datasets. These tools
fill the gap via an allow-listed SSH client.

| Tool | Purpose |
|---|---|
| `proxmox_ssh_wipe_disk` / `proxmox_ssh_init_gpt` | SSH-backed wipe / GPT init |
| `proxmox_zfs_create_dataset` / `proxmox_zfs_destroy_dataset` | Dataset CRUD |
| `proxmox_zfs_set_property` | Set allow-listed properties (compression, atime, recordsize, …) |
| `proxmox_zfs_create_snapshot` | `zfs snapshot [-r] ds@name` |
| `proxmox_zfs_list_datasets` | `zfs list` with optional pool scope / snapshot inclusion |

### Phase 3 — VM disk + ZFS read / scrub / send (7 tools)

| Tool | Purpose |
|---|---|
| `proxmox_move_disk` | Live disk migration QEMU / LXC |
| `proxmox_clone_vm` | Linked or full clone, optional source snapshot |
| `proxmox_list_isos` | ISO inventory |
| `proxmox_zfs_get_property` | Read one or all ZFS properties |
| `proxmox_zfs_pool_status` | `zpool status [-v]` with health line |
| `proxmox_zfs_scrub` | Start / stop a pool scrub |
| `proxmox_zfs_send` | `zfs send → file` or `zfs send \| zfs recv` (replication, raw, incremental) |

### Phase 4 — guest VM shell exec (3 tools)

| Tool | Purpose |
|---|---|
| `proxmox_vm_list_hosts` | Show aliases from `vm_ssh_hosts.json` |
| `proxmox_vm_exec` | Full shell command on a registered VM alias (audit-logged) |
| `proxmox_vm_read_file` | `head -c` wrapper for config/log files |

### Phase 5 — Proxmox host shell exec (1 tool)

| Tool | Purpose |
|---|---|
| `proxmox_host_exec` | Full shell command on the Proxmox host (audit-logged) |

## Safety model

1. **Every write requires `confirm=true`.** Read-only tools have no guard.
2. **Destructive ops also require `i_understand_data_loss=true`.** This applies
   to wipe, destroy pool/VG, recursive zfs destroy, delete_snapshot, and to
   `vm_exec` / `host_exec` when the command matches a destructive regex
   pattern (`rm -rf`, `mkfs`, `dd of=/dev/`, `shutdown`, fork bombs,
   `zpool destroy`, `qm destroy`, etc.).
3. **SSH allow-list.** The Phase 2.5 and 3 SSH-backed tools only invoke
   binaries in a fixed allow-list (`wipefs`, `sgdisk`, `blkdiscard`, `dd`,
   `zfs`, `zpool`, LVM tools, `lsblk`, `nvme`, `smartctl`). Every argument is
   `shlex.quote`d before transmission.
4. **Free-shell exec is audit-logged.** `proxmox_vm_exec` and
   `proxmox_host_exec` write every call (alias, rc, command, stdout/stderr
   previews) to `_vm_ssh_audit.log` / `_host_ssh_audit.log` next to the
   package.

## Requirements

- **Python 3.11+**
- Proxmox VE host reachable over HTTPS (default port 8006)
- Proxmox **API token** (see below)
- Optional: SSH key authorized on the host for Phase 2.5+ tools
- Claude Desktop (or any MCP client)

## 1. Create a Proxmox API token

1. Web UI → **Datacenter → Permissions → API Tokens → Add**
   - **User**: `root@pam` (or a dedicated user)
   - **Token ID**: `mcp-server`
   - **Privilege Separation**: keep enabled
2. Copy the **secret** value (UUID) shown once.
3. Grant the token a role: **Datacenter → Permissions → Add → API Token Permission**
   - **Path**: `/` (or narrower)
   - **Role**: `PVEAdmin` (full) or `PVEVMAdmin` (VM/CT only)
   - **Propagate**: checked

## 2. Install

```bash
git clone https://github.com/ahmetem/proxmox-mcp.git ~/mcp-servers/proxmox-mcp
cd ~/mcp-servers/proxmox-mcp
python3 -m venv .venv
source .venv/bin/activate         # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 3. Configure `.env`

```ini
PROXMOX_HOST=192.168.1.10
PROXMOX_PORT=8006
PROXMOX_USER=root@pam
PROXMOX_TOKEN_NAME=mcp-server
PROXMOX_TOKEN_VALUE=xxxxxxxx-xxxx-...
PROXMOX_VERIFY_SSL=false
PROXMOX_TIMEOUT=30

# Optional: only needed for Phase 2.5+ SSH-backed tools and vm/host exec.
PROXMOX_SSH_HOST=                # defaults to PROXMOX_HOST
PROXMOX_SSH_PORT=22
PROXMOX_SSH_USER=root
PROXMOX_SSH_KEY_PATH=/home/you/.ssh/proxmox_ed25519
PROXMOX_SSH_KNOWN_HOSTS=         # path to known_hosts; "ignore" on a trusted LAN only
PROXMOX_SSH_PASSWORD=            # fallback if key not set
PROXMOX_SSH_TIMEOUT=30
```

## 4. Optional: guest VM SSH registry

`proxmox_vm_exec` / `proxmox_vm_read_file` run commands on guest VMs by
alias. Create `vm_ssh_hosts.json` next to `.env` (the file is gitignored):

```json
{
  "_comment": "Keys starting with _ are treated as comments and ignored.",
  "dockers": {
    "host": "192.168.1.20",
    "port": 22,
    "user": "ahmet",
    "key_path": "/home/you/.ssh/vm_dockers_ed25519",
    "known_hosts": "ignore",
    "description": "Docker host VM 102"
  }
}
```

Claude will resolve `alias="dockers"` to this entry. `known_hosts` may be a
file path, or the literal `"ignore"` on trusted networks.

## 5. Register with Claude Desktop

Edit `claude_desktop_config.json` (Windows: `%APPDATA%\Claude\`,
macOS: `~/Library/Application Support/Claude/`,
Linux: `~/.config/Claude/`):

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

Fully quit Claude Desktop (tray → Quit) and reopen it.

## Example workflows

**Healthcheck:** *"Show me node status and any storage pool above 80%."*
Claude calls `proxmox_list_nodes`, `proxmox_list_storage`, summarises.

**Move a VM disk:** *"Move VM 102's scsi0 from vmdata to nvmepool, keep the
original as unused."*
Claude calls `proxmox_move_disk` with `delete_source=false, confirm=true`.

**ZFS housekeeping:** *"Start a scrub on nvmepool, then show zpool status."*
Claude calls `proxmox_zfs_scrub` then `proxmox_zfs_pool_status`.

**Snapshot before upgrade:** *"Snapshot CT 200 as `pre-pg17-upgrade`, then
run `apt list --upgradable` inside it."*
Claude calls `proxmox_create_snapshot`, then `proxmox_vm_exec` against the
LXC's SSH alias.

## Project structure

```
proxmox-mcp/
├── proxmox_mcp.py                  # compatibility shim for old Claude configs
├── proxmox_mcp/                    # package
│   ├── __init__.py / __main__.py
│   ├── server.py                   # FastMCP entry, TOOLS roster
│   ├── config.py                   # env loading, require_config / require_ssh
│   ├── http_client.py              # async HTTPX wrappers
│   ├── mcp_instance.py             # shared FastMCP
│   ├── models.py                   # shared Pydantic input models
│   ├── format.py                   # fmt_bytes, status_icon, missing_confirm
│   ├── ssh.py                      # allow-listed SSH client
│   ├── host_ssh.py                 # free-shell SSH (host)
│   ├── vm_ssh.py                   # free-shell SSH (guest VMs, registry)
│   └── tools/                      # one module per concern
│       ├── nodes.py vms.py storage.py snapshots.py backups.py
│       ├── disks.py lvm.py zfs.py
│       ├── disks_prepare.py lvm_manage.py zfs_manage.py storage_manage.py
│       ├── ssh_disks.py ssh_zfs.py ssh_zfs_phase3.py
│       ├── vm_disk.py vm_ssh.py host_ssh.py
│       └── __init__.py
├── requirements.txt                # mcp, httpx, pydantic, python-dotenv, asyncssh
├── .env.example                    # template for .env
├── .gitignore                      # excludes .env, vm_ssh_hosts.json, _*.{py,txt,log}
├── LICENSE                         # GPL v3
├── README.md                       # this file
└── README.tr.md                    # Turkish version
```

## Troubleshooting

- **`Authentication failed. Check PROXMOX_TOKEN_VALUE.`** — wrong secret or
  wrong user (must include the realm, e.g. `root@pam`).
- **`Permission denied. Token lacks privileges.`** — add an API Token
  Permission with the right role and `Propagate=checked`.
- **`wipedisk` / `initgpt` rejected with `user != root@pam`** — a Proxmox
  REST quirk for API tokens. Use the SSH-backed equivalents
  (`proxmox_ssh_wipe_disk`, `proxmox_ssh_init_gpt`) instead.
- **`Binary 'X' is not in the SSH allow-list.`** — by design. Use
  `proxmox_host_exec` if you need to run something outside the allow-list.
- **Tools don't appear in Claude Desktop.** — check
  `%APPDATA%\Claude\logs\mcp*.log` (Windows) or `~/Library/Logs/Claude/`
  (macOS) for import errors. Common cause: wrong path or unescaped
  backslashes in `claude_desktop_config.json`.

## Contributing

Issues and PRs welcome. When adding a tool:

1. Match the existing module pattern: a Pydantic input model with
   `model_config = ConfigDict(extra="forbid")`, the `@mcp.tool` decorator
   with annotations, and a `require_config()` (or `require_ssh()`) guard.
2. Tag destructive tools with `destructiveHint: True` and require
   `confirm=True` in the input model. Add `i_understand_data_loss=True`
   to anything irreversible.
3. Register the tool name in `proxmox_mcp/server.py`'s `TOOLS` list and
   add the module to `proxmox_mcp/tools/__init__.py`.
4. Keep this README's tool table in sync.

## License

[GNU General Public License v3.0](./LICENSE) — see `LICENSE` for the full text.
