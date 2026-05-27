"""LXC container service management + log tail helpers.

Both tools are typed shortcuts on top of `proxmox_lxc_exec`, restricted
to two very common operations: managing a systemd service inside an LXC
container and reading log output.

They go through `host_ssh.exec_command` running `pct exec <vmid> -- ...`
from the Proxmox host, just like `proxmox_lxc_exec`. The difference:

  - The inner command is generated from typed parameters, not free-form
    shell text. The agent can't accidentally drop a `; rm -rf /` into a
    service name.
  - Read-only systemctl actions (status, is-active, is-enabled) don't
    require confirm=true. The full `proxmox_lxc_exec` requires confirm
    on every call.
  - Audit log records the action and service distinctly.

For anything outside these two patterns, use `proxmox_lxc_exec` directly.
"""
from __future__ import annotations

import re
import shlex
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from proxmox_mcp import host_ssh
from proxmox_mcp.format import missing_confirm
from proxmox_mcp.mcp_instance import mcp


# Systemd unit naming: alphanumerics, dot, dash, underscore, @, +
_SERVICE_RE = re.compile(r"^[A-Za-z0-9._@+-]+$")
# Absolute paths for file mode. Disallow shell metacharacters.
_PATH_RE = re.compile(r"^/[A-Za-z0-9._/+-]+$")
# Grep pattern: typing characters but no shell metacharacters that could
# break out of the quoted argument. shlex.quote handles the rest.
_GREP_RE = re.compile(r"^[A-Za-z0-9 _.,:@#%/+=()\[\]?*!|<>'\"-]+$")


_ACTIONS_READ_ONLY = frozenset({"status", "is-active", "is-enabled", "show"})
_ACTIONS_STATE_CHANGE = frozenset({
    "start", "stop", "restart", "reload", "try-restart",
    "enable", "disable", "mask", "unmask",
})
ALLOWED_ACTIONS = _ACTIONS_READ_ONLY | _ACTIONS_STATE_CHANGE


class CtServiceActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vmid: int = Field(
        ...,
        description="LXC container ID (e.g. 200, 201, 202).",
        ge=100,
        le=999999999,
    )
    service: str = Field(
        ...,
        description=(
            "Systemd unit name (e.g. 'postgresql', 'n8n', 'docker'). "
            "May contain alphanumerics plus . _ - @ +."
        ),
        min_length=1,
        max_length=128,
    )
    action: str = Field(
        ...,
        description=(
            "Systemctl action. Read-only (no confirm): status, is-active, "
            "is-enabled, show. State-changing (require confirm=true): "
            "start, stop, restart, reload, try-restart, enable, disable, "
            "mask, unmask."
        ),
    )
    confirm: bool = Field(
        default=False,
        description="Required for state-changing actions; ignored for read-only.",
    )
    reason: Optional[str] = Field(default=None, max_length=200)

    @field_validator("service")
    @classmethod
    def _v_service(cls, v: str) -> str:
        if not _SERVICE_RE.fullmatch(v):
            raise ValueError(
                "Service name may contain only alphanumerics and . _ - @ +"
            )
        return v

    @field_validator("action")
    @classmethod
    def _v_action(cls, v: str) -> str:
        if v not in ALLOWED_ACTIONS:
            raise ValueError(
                f"Action {v!r} not allowed. Allowed: {sorted(ALLOWED_ACTIONS)}"
            )
        return v


class CtLogTailInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vmid: int = Field(
        ...,
        description="LXC container ID (e.g. 200, 201, 202).",
        ge=100,
        le=999999999,
    )
    mode: str = Field(
        default="service",
        description=(
            "'service' -> journalctl -u <service>. "
            "'file'    -> tail -n <lines> <path>."
        ),
        pattern="^(service|file)$",
    )
    service: Optional[str] = Field(
        default=None,
        description="Required if mode='service'. Systemd unit name.",
        max_length=128,
    )
    path: Optional[str] = Field(
        default=None,
        description=(
            "Required if mode='file'. Absolute path inside the CT "
            "(e.g. '/var/log/syslog')."
        ),
        max_length=512,
    )
    lines: int = Field(
        default=100,
        description="How many lines to fetch (before optional grep filtering).",
        ge=1,
        le=10000,
    )
    grep: Optional[str] = Field(
        default=None,
        description=(
            "Optional case-insensitive substring filter applied with "
            "`grep -i` on the CT side. Output is `(result) || true` so "
            "an empty match doesn't surface as a non-zero rc."
        ),
        max_length=200,
    )

    @field_validator("service")
    @classmethod
    def _v_service(cls, v):
        if v is None:
            return v
        if not _SERVICE_RE.fullmatch(v):
            raise ValueError("Invalid service name.")
        return v

    @field_validator("path")
    @classmethod
    def _v_path(cls, v):
        if v is None:
            return v
        if not _PATH_RE.fullmatch(v):
            raise ValueError(
                "Path must be absolute (start with /) and may only contain "
                "alphanumerics and . _ - + /."
            )
        return v

    @field_validator("grep")
    @classmethod
    def _v_grep(cls, v):
        if v is None:
            return v
        if not _GREP_RE.fullmatch(v):
            raise ValueError("Grep pattern contains disallowed characters.")
        return v


@mcp.tool(
    name="proxmox_ct_service_action",
    annotations={
        "title": "Manage Systemd Service in LXC Container",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_ct_service_action(params: CtServiceActionInput) -> str:
    """Run `systemctl --no-pager <action> <service>` inside an LXC
    container via `pct exec`.

    Read-only actions (status, is-active, is-enabled, show) run without
    confirmation. State-changing actions (start, stop, restart, reload,
    try-restart, enable, disable, mask, unmask) require confirm=true.

    Output is forwarded verbatim. For `status` it includes the active
    state, recent log lines, and PID. `systemctl status` returns rc=3
    when the unit is inactive but the call itself succeeded; that's
    flagged in the output, not treated as an error.
    """
    state_changing = params.action in _ACTIONS_STATE_CHANGE
    if state_changing and not params.confirm:
        return missing_confirm("proxmox_ct_service_action")

    inner = (
        f"systemctl --no-pager {shlex.quote(params.action)} "
        f"{shlex.quote(params.service)}"
    )
    wrapper = f"pct exec {params.vmid} -- /bin/sh -c {shlex.quote(inner)}"

    try:
        rc, stdout, stderr = await host_ssh.exec_command(wrapper, timeout=30.0)
    except Exception as exc:
        host_ssh.audit_log(
            wrapper, None,
            note=(f"FAILED ct_service_action ct={params.vmid} "
                  f"action={params.action} service={params.service}: "
                  f"{type(exc).__name__}: {exc}"),
        )
        return host_ssh.format_host_ssh_error(exc)

    host_ssh.audit_log(
        wrapper, rc,
        note=(f"ct_service_action ct={params.vmid} "
              f"action={params.action} service={params.service}"
              + (f" reason={params.reason}" if params.reason else "")),
        stdout_preview=stdout[:200],
        stderr_preview=stderr[:200],
    )

    if rc == 0:
        rc_note = ""
    elif rc == 3 and params.action == "status":
        rc_note = " (rc=3 is normal for `status` on an inactive unit)"
    else:
        rc_note = " (non-zero rc)"

    parts = [
        f"CT {params.vmid}: `systemctl {params.action} {params.service}`  "
        f"rc={rc}{rc_note}"
    ]
    if stdout.strip():
        parts.append("```\n" + stdout.rstrip() + "\n```")
    if stderr.strip():
        parts.append("**stderr:**\n```\n" + stderr.rstrip() + "\n```")
    if not stdout.strip() and not stderr.strip():
        parts.append("_(no output)_")
    return "\n\n".join(parts)


@mcp.tool(
    name="proxmox_ct_log_tail",
    annotations={
        "title": "Tail Log from LXC Container",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def proxmox_ct_log_tail(params: CtLogTailInput) -> str:
    """Read the last N lines of a systemd journal or log file from
    inside an LXC container via `pct exec`.

    Two modes:
      - mode='service': journalctl --no-pager -u <service> -n <lines>
      - mode='file':    tail -n <lines> <path>

    Optional `grep` post-filters with `grep -i <pattern>` plus `|| true`
    so an empty match returns rc=0 (no surface error).

    Output capped at 1 MB by host_ssh; for chatty services use a smaller
    `lines` or narrower `grep`.
    """
    if params.mode == "service":
        if not params.service:
            return "Error: mode='service' requires `service` parameter."
        inner_cmd = (
            f"journalctl --no-pager -u {shlex.quote(params.service)} "
            f"-n {params.lines}"
        )
    else:  # file
        if not params.path:
            return "Error: mode='file' requires `path` parameter."
        inner_cmd = f"tail -n {params.lines} {shlex.quote(params.path)}"

    if params.grep:
        inner_cmd = f"({inner_cmd}) | grep -i {shlex.quote(params.grep)} || true"

    wrapper = f"pct exec {params.vmid} -- /bin/sh -c {shlex.quote(inner_cmd)}"

    try:
        rc, stdout, stderr = await host_ssh.exec_command(wrapper, timeout=30.0)
    except Exception as exc:
        host_ssh.audit_log(
            wrapper, None,
            note=(f"FAILED ct_log_tail ct={params.vmid}: "
                  f"{type(exc).__name__}: {exc}"),
        )
        return host_ssh.format_host_ssh_error(exc)

    host_ssh.audit_log(
        wrapper, rc,
        note=(f"ct_log_tail ct={params.vmid} mode={params.mode} "
              f"lines={params.lines}"
              + (f" grep={params.grep!r}" if params.grep else "")),
        stdout_preview=stdout[:200],
        stderr_preview=stderr[:200],
    )

    if rc != 0 and not stdout.strip():
        msg = f"CT {params.vmid}: log tail failed (rc={rc})."
        if stderr.strip():
            msg += f"\n\n**stderr:**\n```\n{stderr.rstrip()}\n```"
        return msg

    target = params.service if params.mode == "service" else (params.path or "?")
    header = (
        f"## CT {params.vmid}: {params.mode} `{target}` "
        f"(last {params.lines} lines"
        + (f", grep `{params.grep}`" if params.grep else "")
        + ")"
    )

    if not stdout.strip():
        return f"{header}\n\n_(no matching output)_"

    return f"{header}\n\n```\n{stdout.rstrip()}\n```"
