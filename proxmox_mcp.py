"""Compatibility shim for clients (e.g. Claude Desktop) that launch the
old single-file entry point ``proxmox_mcp.py``.

The implementation now lives in the ``proxmox_mcp`` package. This file just
re-runs the package's main(), so existing MCP client configs keep working
without changes.
"""
from __future__ import annotations

from proxmox_mcp.server import main

if __name__ == "__main__":
    main()
