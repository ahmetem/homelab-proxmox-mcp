# Proxmox MCP Server

A local [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server
that lets Claude Desktop (or any MCP-compatible client) manage a
**Proxmox VE** cluster through its REST API using token authentication.

Tested with **Proxmox VE 9.1.9** on a single-node setup. Multi-node clusters
should work — every tool accepts a `node` parameter.

> 🇹🇷 Türkçe README için: [README.tr.md](./README.tr.md)

## Features

The package exposes 55 tools across nine phases. The core/"Phase 0"
surface (15 tools) is summarised below by category; advanced tools for
disk preparation, LVM, ZFS, guest SSH, host SSH, LXC exec, and bulk
snapshot cleanup are also registered — run `python proxmox_mcp.py --help`
for the full list.

### Read-only (safe to call automatically)

| Tool | Description |
|---|---|
| `proxmox_list_nodes` | List all nodes in the cluster with status, uptime, CPU and memory usage |
| `proxmox_get_node_status` | Detailed status for one node: CPU model, kernel, load average, disk, swap |
| `proxmox_list_vms` | List every VM and LXC container across the cluster |
| `proxmox_get_vm_status` | Detailed runtime metrics for a single VM/CT |
| `proxmox_list_storage` | Storage pools on a node with usage info |
| `proxmox_list_backups` | Backup files on a storage |
| `proxmox_list_snapshots` | Snapshots for a specific VM/CT |

### Power actions (require `confirm=true`)

| Tool | Description |
|---|---|
| `proxmox_vm_start` | Start a VM or LXC container |
| `proxmox_vm_shutdown` | Graceful ACPI shutdown |
| `proxmox_vm_stop` | Force stop (pull the plug) — may cause data loss |
| `proxmox_vm_reboot` | Graceful reboot, then power cycle if needed |

### Snapshots & backups (require `confirm=true`)

| Tool | Description |
|---|---|
| `proxmox_create_snapshot` | Create a snapshot of a VM/CT |
| `proxmox_rollback_snapshot` | Rollback to a snapshot — data after it is lost |
| `proxmox_create_backup` | Create a backup with selectable mode and compression |
| `proxmox_restore_backup` | Restore a VM/CT from a backup archive. Refuses to overwrite an existing VMID unless `force=true` and `i_understand_data_loss=true`. |

### Configuration (requires `confirm=true`)

| Tool | Description |
|---|---|
| `proxmox_resize_vm` | Change RAM (`memory_mb`) and/or CPU `cores` of a VM/CT |

### Guest exec (Phase 4 / 6 — full shell, audit-logged)

| Tool | Description |
|---|---|
| `proxmox_vm_exec` | Run a shell command on a registered guest VM via SSH (uses `vm_ssh_hosts.json`). Requires `confirm=true`. |
| `proxmox_lxc_exec` | Run a shell command inside an LXC container via `pct exec` from the Proxmox host. No SSH inside the CT needed. Requires `confirm=true`. |

### Bulk ZFS maintenance (Phase 2.5 — SSH-backed)

| Tool | Description |
|---|---|
| `proxmox_zfs_destroy_snapshots_by_pattern` | Bulk-delete ZFS snapshots whose name matches a glob pattern. Two-step: `dry_run=true` (default) lists matches; setting `dry_run=false` with `confirm=true` and `i_understand_data_loss=true` actually deletes. Capped at `max_delete` (default 1000). |

### Built-in safety

All destructive or state-changing actions require `confirm=true`. Tools
that destroy persistent data (snapshot delete, backup restore with
overwrite, bulk snapshot destroy, ZFS dataset destroy, etc.) additionally
require `i_understand_data_loss=true`. The agent must explicitly pass
both flags, which in practice means Claude only fires these after the
user clearly asks for the action. Read-only tools have no such guard.

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
├── proxmox_mcp.py      # The MCP server
├── requirements.txt    # Python dependencies
├── .env.example        # Template for your local .env
├── .gitignore
├── LICENSE             # GPL v3
├── README.md           # This file
└── README.tr.md        # Turkish version
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
