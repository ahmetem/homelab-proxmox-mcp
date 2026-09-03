"""Proxmox VE MCP Server package."""

from proxmox_mcp.server import mcp, main

# Keep in lockstep with pyproject.toml's [project].version -- tests/test_version.py
# fails the build if the two drift. (They did: this literal sat at 1.3.0 while the
# package shipped 1.5.0; caught 2026-09-03.) Reading the version from installed
# package metadata instead was tried and rejected: an editable install keeps the
# version recorded at install time, so it reports a stale number just as silently.
__version__ = "1.5.1"
__all__ = ["mcp", "main", "__version__"]
