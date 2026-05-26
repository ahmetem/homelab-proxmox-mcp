"""Shared FastMCP server instance.

All tool modules import `mcp` from here so they register with the same server.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("proxmox_mcp")
