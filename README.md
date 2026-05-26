# Proxmox MCP Server

A local [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server
that lets Claude Desktop (or any MCP-compatible client) manage a **Proxmox VE**
cluster through its REST API plus an optional SSH layer for the operations
the API doesn't expose.

Tested with **Proxmox VE 9.1.9** on a single-node setup. Multi-node clusters
should work — every tool that needs a node accepts a `node` parameter.

> U0001F1F9U0001F1F7 Türkçe README için: [README.tr.md](./README.tr.md)

## What this gives you

52 tools across five phases. Read-only inventory, VM lifecycle, snapshots,
backups, disk preparation, LVM/ZFS pool create/destroy, cluster storage
management, ZFS dataset/property/snapshot ops via SSH, ZFS replication, full
shell exec on guest VMs and on the Proxmox host — each gated by `confirm=true`
and, where appropriate, `i_understand_data_loss=true`.

### Tool surface, by phase

| Phase | Module(s) | Tools |
|---|---|---|
| 0 — Lifecycle | `nodes`, `vms`, `storage`, `snapshots`, `backups` | 16 |
| 1 — Inventory (read-only) | `disks`, `lvm`, `zfs` | 6 |
| 2 — Disk prep + pool create/destroy + cluster storage | `disks_prepare`, `lvm_manage`, `zfs_manage`, `storage_manage` | 12 |
| 2.5 — SSH-backed (bypass API token restrictions) | `ssh_disks`, `ssh_zfs` | 7 |
| 3 — VM disk ops + ZFS read/scrub/send | `vm_disk`, `ssh_zfs_phase3` | 7 |
| 4 — Guest VM SSH (audit-logged shell exec) | `vm_ssh` | 3 |
| 5 — Proxmox host SSH (audit-logged shell exec) | `host_ssh` | 1 |
| **Total** | 18 modules | **52** |

For the full tool list run `python proxmox_mcp.py --help`.

### Safety model

- **Read-only tools** never require confirmation.
- **State-changing tools** require `confirm=true`. The agent must pass it
  explicitly, which means Claude only fires them after the user clearly
  asks.
- **Destructive tools** (wipe, destroy, delete, force-stop, force shell
  exec matching a destructive pattern) additionally require
  `i_understand_data_loss=true`.
- **SSH allow-listed binaries** (`proxmox_mcp/ssh.py`): only `wipefs`,
  `sgdisk`, `blkdiscard`, `dd`, `zfs`, `zpool`, LVM tools, `lsblk`, `nvme`,
  `smartctl`. Used by `ssh_disks` and the `ssh_zfs*` modules.
- **Free-shell tools** (`proxmox_vm_exec`, `proxmox_host_exec`) have no
  binary allow-list; every call is appended to `_vm_ssh_audit.log` or
  `_host_ssh_audit.log` next to the package. A regex check flags
  destructive commands; overriding requires `i_understand_data_loss=true`.

## Requirements

- Python 3.11+
- A Proxmox VE host reachable over HTTPS (default port 8006)
- A Proxmox API token (for the REST tools)
- An SSH key authorized on the Proxmox host (only required for Phase 2.5
  and Phase 3–5 tools)
- Claude Desktop or any MCP client

## 1. Create a Proxmox API token

1. Web UI → **Datacenter → Permissions → API Tokens → Add**.
2. User `root@pam` (or a dedicated user), Token ID e.g. `mcp-server`.
3. Copy the secret UUID — it is shown once.
4. **Datacenter → Permissions → Add → API Token Permission**: Path `/`,
   Role `PVEAdmin`, Propagate checked. (Narrow this in production.)

## 2. Install

```powershell
git clone https://github.com/ahmetem/proxmox-mcp.git C:\mcp-servers\proxmox-mcp
cd C:\mcp-servers\proxmox-mcp
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Linux/macOS is the same with `python3 -m venv .venv && source .venv/bin/activate`.

## 3. Configure `.env`

Copy `.env.example` to `.env` and fill it in. Minimum (REST only):

```ini
PROXMOX_HOST=192.168.1.21
PROXMOX_PORT=8006
PROXMOX_USER=root@pam
PROXMOX_TOKEN_NAME=mcp-server
PROXMOX_TOKEN_VALUE=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
PROXMOX_VERIFY_SSL=false
PROXMOX_TIMEOUT=30
```

Optional (enables Phase 2.5 + 3–5 tools):

```ini
PROXMOX_SSH_HOST=192.168.1.21        # defaults to PROXMOX_HOST
PROXMOX_SSH_PORT=22
PROXMOX_SSH_USER=root
PROXMOX_SSH_KEY_PATH=C:\Users\you\.ssh\proxmox_ed25519
PROXMOX_SSH_KNOWN_HOSTS=             # path to a known_hosts file, or 'ignore' on a trusted LAN
PROXMOX_SSH_TIMEOUT=30
```

Key auth is strongly preferred. Set `PROXMOX_SSH_KNOWN_HOSTS=ignore` only on
a trusted local network; the server logs a stderr warning when it sees that.

### `vm_ssh_hosts.json` (Phase 4 only)

For `proxmox_vm_exec` / `proxmox_vm_read_file` the server reads a JSON
registry of guest VM SSH targets from `vm_ssh_hosts.json` next to `.env`.
Not committed to git. Format:

```json
{
  "web01": {
    "host": "192.168.1.50",
    "port": 22,
    "user": "deploy",
    "key_path": "C:\\Users\\you\\.ssh\\web01_ed25519",
    "known_hosts": "ignore",
    "description": "Web app, NGINX + Node"
  },
  "db01": {
    "host": "192.168.1.51",
    "port": 22,
    "user": "postgres",
    "key_path": "C:\\Users\\you\\.ssh\\db01_ed25519",
    "description": "Postgres 16"
  }
}
```

Use `proxmox_vm_list_hosts` from chat to confirm which aliases are visible.

## 4. Smoke test

```powershell
python proxmox_mcp.py --help
```

You should see the full 52-tool list and a clean exit.

## 5. Register with Claude Desktop

`%APPDATA%\Claude\claude_desktop_config.json`:

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

The top-level `proxmox_mcp.py` is a compatibility shim; you can equivalently
use `args: ["-m", "proxmox_mcp"]`. Fully quit Claude Desktop and reopen.

## Examples

```
List my Proxmox nodes.
# proxmox_list_nodes

What disks does the host see? Show wearout.
# proxmox_list_disks

Create a snapshot of VM 102 called pre-upgrade.
# proxmox_create_snapshot(confirm=true)

Move scsi0 of VM 102 from vmdata to nvmepool, keep the original.
# proxmox_move_disk(confirm=true, delete_source=false)

Start a scrub on nvmepool.
# proxmox_zfs_scrub(confirm=true)

Replicate nvmepool/data@daily-2025-11 to vmdata/backup.
# proxmox_zfs_send(replication=true, confirm=true)

Reboot the dockers VM.
# proxmox_vm_exec(alias="dockers", command="sudo systemctl reboot",
#                 confirm=true, i_understand_data_loss=true)
```

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `PROXMOX_HOST` | — (required) | Proxmox host IP/hostname |
| `PROXMOX_PORT` | `8006` | API port |
| `PROXMOX_USER` | — (required) | Token owner (e.g. `root@pam`) |
| `PROXMOX_TOKEN_NAME` | — (required) | API token ID |
| `PROXMOX_TOKEN_VALUE` | — (required) | API token secret (UUID) |
| `PROXMOX_VERIFY_SSL` | `false` | Verify the API TLS cert |
| `PROXMOX_TIMEOUT` | `30` | HTTP timeout in seconds |
| `PROXMOX_SSH_HOST` | `PROXMOX_HOST` | SSH target (only if SSH tools are used) |
| `PROXMOX_SSH_PORT` | `22` | SSH port |
| `PROXMOX_SSH_USER` | `root` | SSH username |
| `PROXMOX_SSH_KEY_PATH` | — | Absolute path to private key (preferred auth) |
| `PROXMOX_SSH_PASSWORD` | — | Fallback if no key is set |
| `PROXMOX_SSH_KNOWN_HOSTS` | — | Path to known_hosts file, or `ignore` |
| `PROXMOX_SSH_TIMEOUT` | `30` | SSH timeout in seconds |

## Project structure

```
proxmox-mcp/
├── proxmox_mcp.py             # Compatibility shim (calls proxmox_mcp.server:main)
├── proxmox_mcp/
│   ├── __init__.py            # Exposes mcp, main
│   ├── __main__.py            # `python -m proxmox_mcp`
│   ├── server.py              # Entry point + TOOLS list
│   ├── config.py              # .env loading, require_config(), require_ssh()
│   ├── format.py              # fmt_bytes, status_icon, missing_confirm, …
│   ├── http_client.py         # Async REST helpers
│   ├── mcp_instance.py        # Shared FastMCP instance
│   ├── models.py              # Shared Pydantic input models
│   ├── ssh.py                 # Allow-listed SSH client
│   ├── host_ssh.py            # Free-shell SSH on the Proxmox host
│   ├── vm_ssh.py              # Free-shell SSH on guest VMs (registry-based)
│   └── tools/                 # 18 tool modules grouped by phase
├── requirements.txt           # mcp, httpx, pydantic, python-dotenv, asyncssh
├── .env.example               # Template; copy to .env and fill in
├── .gitignore                 # Excludes .env, vm_ssh_hosts.json, audit logs
├── LICENSE                    # GPL v3
├── README.md                  # This file
└── README.tr.md               # Turkish version
```

## Troubleshooting

- **"Authentication failed. Check PROXMOX_TOKEN_VALUE."** — token secret
  wrong, or user mismatch. The token user must match (e.g. `root@pam`,
  not just `root`).
- **"Permission denied. Token lacks privileges."** — privilege separation
  on, no role assigned. Add an **API Token Permission**.
- **`wipedisk`/`initgpt` returns "user != root@pam"** — Proxmox quirk: the
  REST endpoints refuse API tokens. Use the SSH variants
  `proxmox_ssh_wipe_disk` / `proxmox_ssh_init_gpt`.
- **`zfs`/`zpool` commands return "binary not in allow-list"** — the SSH
  module enforces an allow-list. If you genuinely need another binary,
  edit `ALLOWED_BINARIES` in `proxmox_mcp/ssh.py` and submit a PR.
- **`vm_exec` says "alias not in vm_ssh_hosts.json"** — add the alias to
  the JSON file next to `.env`.
- **Tools missing in Claude Desktop** — check
  `%APPDATA%\Claude\logs\mcp*.log` (Windows) or
  `~/Library/Logs/Claude/mcp*.log` (macOS) for the import error.

## Contributing

Issues and PRs welcome. For a new tool:

1. Pick the right phase module under `proxmox_mcp/tools/`, or add a new
   module and reference it in `proxmox_mcp/tools/__init__.py`.
2. Follow the existing pattern: Pydantic input model with strict
   regex/length validation, `require_config()` or `require_ssh()` guard,
   `confirm=true` on writes, `i_understand_data_loss=true` on destructive
   ops.
3. Append the tool name to the `TOOLS` list in `proxmox_mcp/server.py`.
4. Update both READMEs.

## License

[GNU General Public License v3.0](./LICENSE) — see the `LICENSE` file for the
full text.
