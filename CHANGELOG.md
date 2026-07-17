# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] - 2026-07-17

### Added
- **Audit replay** — `proxmox_audit_verify` gains a `tail=N` parameter that
  lists the most recent N recorded audit entries alongside the INTACT/BROKEN
  chain verdict (backed by a new read-only `audit.read_entries()`). Verifying
  the SSH exec trail and reviewing what ran is now a single call.
- **`tests/test_safety_gates.py`** — bypass/fuzz tests proving the safety gates
  cannot be circumvented: `extra="forbid"` rejects a typo'd gate field
  (`confirmed=true`) and malformed values (`confirm="maybe"`, `confirm=2`,
  `i_understand_data_loss="yep"`); and a refused mutation (missing `confirm`,
  missing `i_understand_data_loss` on an overwrite restore, or `dry_run`) makes
  **no** HTTP write call (verified with a POST spy). Suite is now 54 tests.

### Changed
- `proxmox_audit_verify` title and description updated for the verify **+
  replay** capability; it stays read-only.

## [1.2.0] - 2026-07-17

### Added
- **Guest creation from scratch** — closes the biggest capability gap vs. other
  Proxmox MCP servers (they had create; we only had clone):
  - `proxmox_create_vm` — QEMU VM with core hardware (cores/sockets/memory/
    ostype), one virtio-scsi boot disk, one virtio NIC, and an optional install
    ISO (boots from the CD first). Requires `confirm=true`.
  - `proxmox_create_container` — LXC from a template with rootfs, one NIC (dhcp
    or a static CIDR), root access via `password` and/or `ssh_public_key`,
    unprivileged by default, optional `nesting`. Requires `confirm=true`.
  - Medium scope by design: cloud-init, multi-disk and multi-NIC are left out
    for now; add them afterwards with the config/resize tools.
- **`dry_run` preview** on the high-consequence mutations `proxmox_create_vm`,
  `proxmox_create_container`, `proxmox_clone_vm`, and `proxmox_restore_backup`.
  With `dry_run=true` the tool returns the exact endpoint + payload it would
  send (secrets masked) instead of executing. `proxmox_restore_backup` still
  runs its read-only existence probe first, so the preview reports whether the
  restore would be a fresh create or an overwrite.
- **Tamper-evident audit log** (`proxmox_mcp/audit.py`). The host- and
  guest-SSH audit logs are now hash-chained: each entry carries `prev` + `hash`
  (SHA-256, or HMAC-SHA256 when `PROXMOX_AUDIT_HMAC_KEY` is set), so altering,
  deleting (including from the head of the chain) or reordering any line is
  detectable.
  - `proxmox_audit_verify` — new read-only tool that recomputes the chain and
    reports INTACT / BROKEN with the first offending line. Pre-existing legacy
    lines (from before this release) are tolerated and don't break the chain
    that follows them.
- `tests/test_guest_create.py` (create models, dry_run previews, secret
  masking, gateway/ip validation, restore preview via a monkeypatched probe)
  and `tests/test_audit.py` (chain integrity, tamper / mid- and head-deletion
  detection, HMAC mode, wrong-key rejection).
- `pyproject.toml` — PyPI/uvx packaging with a console entry point
  (`pve-mcp` = `proxmox_mcp.server:main`), enabling one-command install via
  the `proxmox-mcp-suite` Claude Code marketplace.

### Changed
- Shared `mask_secrets()` + `dry_run_preview()` helpers added to
  `proxmox_mcp/format.py`, used by all four dry_run-capable tools.
- Tool count 49 → 52.

### Security
- Optional `PROXMOX_AUDIT_HMAC_KEY` upgrades the audit chain from
  integrity-evident (catches accidental corruption, truncation and deletion)
  to tamper-evident (an attacker who cannot read the key cannot forge a valid
  chain).

## [1.1.0] - 2026-07-04

### Added
- **`proxmox_host_read_exec` read-only allow-list greatly expanded** for host
  diagnostics (all additions are genuinely non-mutating; the safety model —
  no shell metacharacters, subcommand restriction for mutable binaries, and the
  `is_destructive` backstop — is unchanged). New pure-read binaries: LVM
  reporting (`pvs`, `lvs`, `vgs`, `pvdisplay`, `lvdisplay`, `vgdisplay`),
  hardware (`lspci`, `lsusb`, `lsscsi`, `lsmod`, `lshw`, `dmidecode`),
  process/network (`lsof`, `pgrep`, `pidof`, `vmstat`, `iostat`, `mpstat`,
  `numastat`), users (`id`, `groups`, `whoami`, `getent`, `who`, `w`, `last`),
  text/file (`grep`, `egrep`, `zgrep`, `zcat`, `getfacl`, `nl`, `tac`, `tree`),
  ZFS `zdb`, packages (`dpkg-query`, `apt-cache`), and `dmesg`. New
  subcommand-restricted: `proxmox-boot-tool status`, `nvme <read-subcmds>`,
  `apt list`, `apt-mark show*`, `chronyc <read-subcmds>`, `coredumpctl
  list/info`; extended read subcommands on `zpool`, `zfs`, `systemctl`, `qm`,
  `pct`, `pvesm`.
- **`_DENIED_FLAGS` guard** blocks the write/state escape hatches on otherwise
  read-only binaries: `dmesg -C/-c/-n/--console-*/-w` (clear ring buffer / set
  console level / follow) and `dmidecode --dump-bin/--dump` (writes SMBIOS to a
  file). Mutating LVM/nvme/apt operations stay refused (not allow-listed, or the
  wrong subcommand). Expanded `tests/test_host_read.py` (11/11 pass).

## [1.0.0] - 2026-07-03

Token-efficiency and effectiveness overhaul. **BREAKING**: 16 tools were
consolidated into 7 action-based tools (64 → 49 total); callers using the
old names must switch to the new `action=` form.

### Changed (BREAKING — tool consolidation)
- `proxmox_vm_start` / `_vm_shutdown` / `_vm_stop` / `_vm_reboot` →
  **`proxmox_vm_power`** with `action='start'|'shutdown'|'stop'|'reboot'`.
- `proxmox_create_snapshot` / `_rollback_snapshot` / `_delete_snapshot` →
  **`proxmox_snapshot`** with `action='create'|'rollback'|'delete'`
  (delete keeps the `i_understand_data_loss` gate).
- `proxmox_disk_init_gpt` / `_wipe_disk` / `_ssh_init_gpt` / `_ssh_wipe_disk`
  → **`proxmox_disk_prepare`** with `action='wipe'|'init_gpt'` and
  `via='auto'|'api'|'ssh'`; `auto` (default) tries the REST endpoint and
  transparently falls back to SSH when Proxmox rejects the API token
  ("user != root@pam") — the manual API→SSH retry dance is gone.
- `proxmox_create_lvm_vg` / `_create_lvm_thin` / `_destroy_lvm_vg` /
  `_destroy_lvm_thin` → **`proxmox_lvm_manage`** with
  `action='create_vg'|'create_thin'|'destroy_vg'|'destroy_thin'`.
- `proxmox_create_zfs_pool` / `_destroy_zfs_pool` →
  **`proxmox_zfs_pool_manage`** with `action='create'|'destroy'`.
- `proxmox_zfs_create_dataset` / `_zfs_destroy_dataset` →
  **`proxmox_zfs_dataset`** with `action='create'|'destroy'`.
- `proxmox_zfs_get_property` / `_zfs_set_property` →
  **`proxmox_zfs_property`** with `action='get'|'set'`.
- `proxmox_add_zfs_storage` / `_add_dir_storage` / `_remove_storage` →
  **`proxmox_storage_config`** with `action='add_zfs'|'add_dir'|'remove'`.
- Per-action required parameters are enforced by model validators
  (`tests/test_inputs.py`); per-action confirm / data-loss gates are
  preserved exactly.

### Added
- **`proxmox_health_overview`** — one-call compact summary (node load,
  guest states, storage fill levels with ≥85% warnings, ZFS pool health);
  the four API calls run concurrently. Replaces the usual 4–6-call
  "how is the server?" sequence.
- **`wait_seconds`** parameter (0–600, default 0) on task-launching tools:
  `proxmox_vm_power`, `proxmox_snapshot`, `proxmox_create_backup`,
  `proxmox_restore_backup`, `proxmox_move_disk`, `proxmox_clone_vm`,
  `proxmox_lvm_manage`, `proxmox_zfs_pool_manage`, `proxmox_disk_prepare`.
  The server polls the UPID task status (1s → 5s backoff) and appends the
  final `exitstatus` to the response, eliminating the follow-up status
  round trip (`http_client.wait_for_task`).
- **List filters** — `proxmox_list_vms(node=, vmid=, status=, guest_type=)`,
  `proxmox_list_backups(vmid=, limit=)` (now sorted newest-first with a
  "showing N of M" note).
- **`fields` projection** — on JSON-capable read tools
  (`fields=['vmid','name','status']` keeps only those keys per object;
  `format.project_fields`). Available on nodes/VM/storage/disk/LVM/ZFS
  listing tools and `proxmox_list_backup_jobs`.
- 30s TTL cache for VM-type resolution (`/cluster/resources` lookup) used
  by `proxmox_vm_power` and `proxmox_get_vm_status` — one fetch now serves
  a burst of related calls.
- `tests/test_inputs.py` — validation tests for the consolidated
  action-based input models.

### Changed
- `compact_json` now truncates oversized **lists at the item level** and
  appends a `{"_truncated_items": N}` marker, so truncated output stays
  valid JSON (previously a hard character cut produced invalid JSON).
  Only a single non-list payload that alone exceeds the limit still falls
  back to the hard string cap.
- Docstring diet across all tools: removed `Returns:` boilerplate from
  tool descriptions (schema tokens the model doesn't need); behavioral
  notes (confirm gates, side effects) kept.

### Removed
- `proxmox_mcp/tools/ssh_disks.py` (merged into `disks_prepare.py`).

## [0.9.1] - 2026-07-02

### Fixed
- Read-only listing tools that take no required arguments
  (`proxmox_list_nodes`, `proxmox_list_vms`, `proxmox_list_backup_jobs`,
  `proxmox_list_cluster_storage`) could not be called without arguments: they
  wrapped an all-optional Pydantic model in a single `params` parameter, and
  FastMCP marks such a parameter as `required` in the tool's JSON schema. A
  caller invoking them with no arguments got a `params Field required`
  validation error; only an explicit `params: {}` worked. Gave `params` a
  default (`FormatInput = FormatInput()`) on those four tools so `params` drops
  out of the schema's `required` list and the tools are callable with no args
  (defaulting to markdown output). Tools with genuinely required fields (e.g.
  `proxmox_get_node_status`) are unchanged and still require `params`.

## [0.9.0] - 2026-07-02

### Added
- `proxmox_host_read_exec` — an allow-listed, **read-only** counterpart to
  `proxmox_host_exec`. Runs a single non-mutating host command (cat, tail,
  journalctl, `zpool status`, `systemctl status`, `pct`/`qm` config, …) with no
  `confirm` required, so it can safely be granted to read-only agents for host
  diagnostics. Enforced in `proxmox_mcp/tools/host_read.py` by four layers:
  shell-metacharacter rejection (no pipes / redirects / substitution /
  chaining), a binary + read-subcommand allow-list, mutating-flag rejection
  (`tail -f`, `journalctl --rotate/--vacuum*`, `smartctl -t`), and the shared
  `host_ssh.is_destructive` detector as a final backstop.
- `tests/test_host_read.py` — allow/deny tests for the command validator (safe
  reads, metacharacter escapes, non-allow-listed binaries, mutating
  subcommands, hanging/state-changing flags).

### Notes
- `proxmox_host_exec` (arbitrary shell, `confirm` required) is unchanged and
  remains the escape hatch for anything outside the read-only allow-list.

## [0.8.0] - 2026-07-01

### Added
- `compact_json()` and `truncate()` helpers in `proxmox_mcp/format.py`. They
  serialize to compact JSON (no indent whitespace) and hard-cap response length
  (default 8000 chars) so a pathologically large payload can't flood the LLM
  context.
- `tests/` directory with char-budget tests for the new helpers. Runnable with
  pytest (`python -m pytest tests/`) or standalone (`python tests/test_format.py`).

### Changed
- All opt-in `response_format=json` branches across the tool modules (20 call
  sites in 14 files) now return `compact_json(...)` instead of
  `json.dumps(..., indent=2)`. This cuts ~30-40% of the bytes on the JSON path
  and adds a length cap to the only previously-unbounded output. The default
  Markdown output is unchanged.
- Removed the now-unused `import json` from the tool modules that only used it
  for the replaced dumps.

### Notes
- This is the first tracked release. Earlier development (Phases 1-7, 63 tools)
  predates this changelog and is not enumerated here; see the git history.
