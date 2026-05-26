"""VM SSH tools (full shell exec).

The user has explicitly opted into unrestricted command execution on guest
VMs registered in vm_ssh_hosts.json (see chat log). Safety reduces to:

- confirm=true on every exec
- destructive-pattern detection -> requires i_understand_data_loss=true
- every call written to _vm_ssh_audit.log

This is intentionally less restrictive than the Proxmox-host SSH module.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from proxmox_mcp import vm_ssh
from proxmox_mcp.format import missing_confirm, missing_data_loss_ack
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import ResponseFormat


_ALIAS_RE = r"^[A-Za-z][A-Za-z0-9_.-]*$"


class VmListHostsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class VmExecInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str = Field(
        ...,
        description="VM alias from vm_ssh_hosts.json (e.g. 'dockers').",
        min_length=1, max_length=64, pattern=_ALIAS_RE,
    )
    command: str = Field(
        ...,
        description=(
            "Shell command to execute on the VM. Runs under the user's "
            "login shell, so pipes, redirects, heredocs all work. Output "
            "is capped at 1 MB per stream."
        ),
        min_length=1, max_length=16384,
    )
    timeout: float = Field(
        default=60.0,
        description="Per-call timeout in seconds.",
        ge=1.0, le=900.0,
    )
    confirm: bool = Field(
        default=False,
        description="Required. Set after confirming the command with the user.",
    )
    i_understand_data_loss: bool = Field(
        default=False,
        description=(
            "Required only when the command matches a destructive pattern "
            "(rm -rf, mkfs, dd of=/dev/..., shutdown, etc.)."
        ),
    )
    reason: Optional[str] = Field(default=None, max_length=200)


class VmReadFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str = Field(..., min_length=1, max_length=64, pattern=_ALIAS_RE)
    path: str = Field(
        ...,
        description="Absolute file path on the VM (e.g. '/etc/os-release').",
        min_length=1, max_length=512,
        pattern=r"^/[^\x00\s]*$",
    )
    max_bytes: int = Field(
        default=65536,
        description="Read at most this many bytes (1..1048576).",
        ge=1, le=1_048_576,
    )

    @field_validator("path")
    @classmethod
    def _no_shell_chars(cls, v: str) -> str:
        bad = set("`$;|&<>\n\r\"'\\")
        if any(c in bad for c in v):
            raise ValueError(f"Path contains shell metacharacters: {v!r}")
        return v


@mcp.tool(
    name="proxmox_vm_list_hosts",
    annotations={
        "title": "List VM SSH Aliases",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": False,
    },
)
async def proxmox_vm_list_hosts(params: VmListHostsInput) -> str:
    """List VM aliases configured in vm_ssh_hosts.json.

    Use this to discover which guests are reachable via proxmox_vm_exec /
    proxmox_vm_read_file.
    """
    try:
        reg = vm_ssh.load_registry()
    except Exception as exc:
        return vm_ssh.format_vm_ssh_error(exc)

    if params.response_format == ResponseFormat.JSON:
        import json as _json
        return _json.dumps(
            {a: {"host": s.host, "port": s.port, "user": s.user,
                 "description": s.description}
             for a, s in reg.items()},
            indent=2,
        )

    if not reg:
        return "_No VM aliases configured._ Edit vm_ssh_hosts.json to add hosts."

    lines = [
        "## VM SSH registry",
        "",
        "| Alias | Host:Port | User | Description |",
        "| --- | --- | --- | --- |",
    ]
    for alias, spec in sorted(reg.items()):
        lines.append(
            f"| `{alias}` | `{spec.host}:{spec.port}` | `{spec.user}` | "
            f"{spec.description} |"
        )
    return "\n".join(lines)


@mcp.tool(
    name="proxmox_vm_exec",
    annotations={
        "title": "Execute Shell Command on Guest VM",
        "readOnlyHint": False, "destructiveHint": True,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_vm_exec(params: VmExecInput) -> str:
    """Run a shell command on a registered guest VM and return its output.

    This is a full shell exec: no command allow-list, pipes/redirects work.
    Requires confirm=true on every call. Commands that match a destructive
    pattern (rm -rf, mkfs, dd of=/dev/..., shutdown, fork bombs, etc.) also
    require i_understand_data_loss=true.

    Every call is recorded in _vm_ssh_audit.log alongside this file.
    """
    if not params.confirm:
        return missing_confirm("proxmox_vm_exec")

    danger = vm_ssh.is_destructive(params.command)
    if danger and not params.i_understand_data_loss:
        return (
            f"Refused: command matches destructive pattern {danger!r}. "
            "Re-run with i_understand_data_loss=true if this is intentional."
        )

    try:
        rc, stdout, stderr = await vm_ssh.exec_command(
            params.alias, params.command, timeout=params.timeout
        )
    except Exception as exc:
        vm_ssh.audit_log(
            params.alias, params.command, None,
            note=f"FAILED: {type(exc).__name__}: {exc}",
        )
        return vm_ssh.format_vm_ssh_error(exc)

    vm_ssh.audit_log(
        params.alias, params.command, rc,
        note=("DESTRUCTIVE" if danger else "") + (f" reason={params.reason}" if params.reason else ""),
        stdout_preview=stdout[:200],
        stderr_preview=stderr[:200],
    )

    parts = [f"`{params.alias}`  rc={rc}"]
    if stdout.strip():
        parts.append("**stdout:**\n```\n" + stdout.rstrip() + "\n```")
    if stderr.strip():
        parts.append("**stderr:**\n```\n" + stderr.rstrip() + "\n```")
    if not stdout.strip() and not stderr.strip():
        parts.append("_(no output)_")
    return "\n\n".join(parts)


@mcp.tool(
    name="proxmox_vm_read_file",
    annotations={
        "title": "Read File from Guest VM",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def proxmox_vm_read_file(params: VmReadFileInput) -> str:
    """Read up to max_bytes from a file on the VM (uses head -c via SSH).

    Convenience wrapper around proxmox_vm_exec for the common case of
    grabbing a config/log file. No confirm needed — read-only.
    """
    import shlex
    quoted = shlex.quote(params.path)
    cmd = f"head -c {int(params.max_bytes)} -- {quoted}"

    try:
        rc, stdout, stderr = await vm_ssh.exec_command(
            params.alias, cmd, timeout=30.0
        )
    except Exception as exc:
        vm_ssh.audit_log(params.alias, cmd, None,
                         note=f"FAILED: {type(exc).__name__}: {exc}")
        return vm_ssh.format_vm_ssh_error(exc)

    vm_ssh.audit_log(
        params.alias, cmd, rc,
        note=f"read_file path={params.path}",
        stdout_preview=stdout[:200],
        stderr_preview=stderr[:200],
    )

    if rc != 0:
        return (
            f"Error reading `{params.path}` from `{params.alias}` (rc={rc})."
            + (f"\nstderr: {stderr.strip()}" if stderr.strip() else "")
        )
    return (
        f"## `{params.path}` on `{params.alias}` ({len(stdout)} bytes)\n"
        f"```\n{stdout}\n```"
    )
