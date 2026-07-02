# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
