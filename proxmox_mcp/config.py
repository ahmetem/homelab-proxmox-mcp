"""Configuration loaded from environment variables."""
from __future__ import annotations

import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

PROXMOX_HOST = os.getenv("PROXMOX_HOST", "").strip()
PROXMOX_PORT = os.getenv("PROXMOX_PORT", "8006").strip()
PROXMOX_USER = os.getenv("PROXMOX_USER", "").strip()
PROXMOX_TOKEN_NAME = os.getenv("PROXMOX_TOKEN_NAME", "").strip()
PROXMOX_TOKEN_VALUE = os.getenv("PROXMOX_TOKEN_VALUE", "").strip()
PROXMOX_VERIFY_SSL = os.getenv("PROXMOX_VERIFY_SSL", "false").lower() == "true"
PROXMOX_TIMEOUT = float(os.getenv("PROXMOX_TIMEOUT", "30"))

# --- Optional SSH config (used by ssh-backed tools) ---
PROXMOX_SSH_HOST = os.getenv("PROXMOX_SSH_HOST", "").strip() or PROXMOX_HOST
PROXMOX_SSH_PORT = int(os.getenv("PROXMOX_SSH_PORT", "22"))
PROXMOX_SSH_USER = os.getenv("PROXMOX_SSH_USER", "root").strip()
PROXMOX_SSH_KEY_PATH = os.getenv("PROXMOX_SSH_KEY_PATH", "").strip()
PROXMOX_SSH_PASSWORD = os.getenv("PROXMOX_SSH_PASSWORD", "")
PROXMOX_SSH_KNOWN_HOSTS = os.getenv("PROXMOX_SSH_KNOWN_HOSTS", "").strip()
PROXMOX_SSH_TIMEOUT = float(os.getenv("PROXMOX_SSH_TIMEOUT", "30"))


def ssh_available() -> bool:
    """True if at least one auth method (key or password) is configured."""
    return bool(PROXMOX_SSH_KEY_PATH) or bool(PROXMOX_SSH_PASSWORD)


def require_ssh() -> Optional[str]:
    """Return None if SSH is configured, else an error message."""
    if not PROXMOX_SSH_HOST:
        return "Error: PROXMOX_SSH_HOST (or PROXMOX_HOST) not set."
    if not ssh_available():
        return (
            "Error: SSH auth not configured. Set PROXMOX_SSH_KEY_PATH "
            "(preferred) or PROXMOX_SSH_PASSWORD in .env."
        )
    return None


def require_config() -> Optional[str]:
    """Return None if config is complete, else an error message listing missing vars."""
    missing = []
    if not PROXMOX_HOST:
        missing.append("PROXMOX_HOST")
    if not PROXMOX_USER:
        missing.append("PROXMOX_USER")
    if not PROXMOX_TOKEN_NAME:
        missing.append("PROXMOX_TOKEN_NAME")
    if not PROXMOX_TOKEN_VALUE:
        missing.append("PROXMOX_TOKEN_VALUE")
    if missing:
        return f"Error: Missing env vars: {', '.join(missing)}"
    return None


def base_url() -> str:
    return f"https://{PROXMOX_HOST}:{PROXMOX_PORT}/api2/json"


def auth_header() -> dict[str, str]:
    return {
        "Authorization": (
            f"PVEAPIToken={PROXMOX_USER}!{PROXMOX_TOKEN_NAME}={PROXMOX_TOKEN_VALUE}"
        )
    }
