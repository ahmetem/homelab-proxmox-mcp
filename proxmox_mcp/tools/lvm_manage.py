"""Phase 2: LVM and LVM-thin pool creation/removal (single manage tool).

Backed by Proxmox REST endpoints:
  - POST   /nodes/{node}/disks/lvm        body: name, device, add_storage
  - DELETE /nodes/{node}/disks/lvm/{name} query: cleanup-config, cleanup-disks
  - POST   /nodes/{node}/disks/lvmthin    body: name, device, add_storage
  - DELETE /nodes/{node}/disks/lvmthin/{name}?volume-group=...
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from proxmox_mcp import http_client
from proxmox_mcp.config import require_config
from proxmox_mcp.format import missing_confirm, missing_data_loss_ack
from proxmox_mcp.mcp_instance import mcp
from proxmox_mcp.models import WAIT_DESC


_VG_NAME = r"^[A-Za-z0-9_+.][A-Za-z0-9_+.-]*$"


class LvmManageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(
        ...,
        description=(
            "'create_vg' / 'create_thin' (need device; confirm) or "
            "'destroy_vg' / 'destroy_thin' (confirm + i_understand_data_loss)."
        ),
        pattern="^(create_vg|create_thin|destroy_vg|destroy_thin)$",
    )
    node: str = Field(..., min_length=1)
    name: str = Field(
        ...,
        description="Volume group / thin pool name.",
        min_length=1, max_length=64, pattern=_VG_NAME,
    )
    device: Optional[str] = Field(
        default=None,
        description=(
            "Create actions only: block device for the PV / thin pool "
            "(e.g. '/dev/nvme0n1'). Disk must be empty — wipe first if needed."
        ),
        max_length=64,
        pattern=r"^/dev/[A-Za-z0-9/_-]+$",
    )
    volume_group: Optional[str] = Field(
        default=None,
        description="destroy_thin only: VG that contains the thin pool.",
        max_length=64, pattern=_VG_NAME,
    )
    add_storage: bool = Field(
        default=True,
        description="Create actions: also register as PVE storage (default).",
    )
    cleanup_config: bool = Field(
        default=True,
        description="Destroy actions: also remove matching PVE storage entries.",
    )
    cleanup_disks: bool = Field(
        default=False,
        description=(
            "Destroy actions: also wipe the underlying disks afterwards. "
            "Extra dangerous — set only when you actually want to free the disks."
        ),
    )
    confirm: bool = Field(default=False)
    i_understand_data_loss: bool = Field(
        default=False, description="Required for destroy actions."
    )
    wait_seconds: int = Field(default=0, ge=0, le=600, description=WAIT_DESC)
    reason: Optional[str] = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _check_required(self):
        if self.action in ("create_vg", "create_thin") and not self.device:
            raise ValueError(f"action='{self.action}' requires 'device'.")
        if self.action == "destroy_thin" and not self.volume_group:
            raise ValueError("action='destroy_thin' requires 'volume_group'.")
        return self


@mcp.tool(
    name="proxmox_lvm_manage",
    annotations={
        "title": "Create / Destroy LVM VG or Thin Pool",
        "readOnlyHint": False, "destructiveHint": True,
        "idempotentHint": False, "openWorldHint": True,
    },
)
async def proxmox_lvm_manage(params: LvmManageInput) -> str:
    """Create or destroy an LVM volume group / LVM-thin pool.

    Gates: create actions require confirm=true (device must be empty — use
    proxmox_disk_prepare first if needed). Destroy actions require BOTH
    confirm=true AND i_understand_data_loss=true; all LVs and their data are
    irretrievable. Set wait_seconds>0 for the task result inline.
    """
    cfg = require_config()
    if cfg:
        return cfg
    if not params.confirm:
        return missing_confirm(f"proxmox_lvm_manage (action={params.action})")

    destroy = params.action.startswith("destroy")
    if destroy and not params.i_understand_data_loss:
        return missing_data_loss_ack(f"proxmox_lvm_manage (action={params.action})")

    try:
        if params.action == "create_vg":
            payload = {"name": params.name, "device": params.device,
                       "add_storage": 1 if params.add_storage else 0}
            task_id = await http_client.post(
                f"/nodes/{params.node}/disks/lvm", data=payload)
            what = f"VG '{params.name}' creation on `{params.device}`"
        elif params.action == "create_thin":
            payload = {"name": params.name, "device": params.device,
                       "add_storage": 1 if params.add_storage else 0}
            task_id = await http_client.post(
                f"/nodes/{params.node}/disks/lvmthin", data=payload)
            what = f"Thin pool '{params.name}' creation on `{params.device}`"
        elif params.action == "destroy_vg":
            query = {"cleanup-config": 1 if params.cleanup_config else 0,
                     "cleanup-disks": 1 if params.cleanup_disks else 0}
            task_id = await http_client.delete(
                f"/nodes/{params.node}/disks/lvm/{params.name}", params=query)
            what = f"Destroy of VG '{params.name}'"
        else:  # destroy_thin
            query = {"volume-group": params.volume_group,
                     "cleanup-config": 1 if params.cleanup_config else 0,
                     "cleanup-disks": 1 if params.cleanup_disks else 0}
            task_id = await http_client.delete(
                f"/nodes/{params.node}/disks/lvmthin/{params.name}", params=query)
            what = f"Destroy of thin pool '{params.name}'"
    except Exception as exc:
        return http_client.format_http_error(exc)

    storage_msg = ""
    if params.action.startswith("create") and params.add_storage:
        storage_msg = " Also registered as PVE storage."
    suffix = await http_client.wait_for_task(params.node, task_id, params.wait_seconds)
    return f"OK: {what} started ({params.node}).{storage_msg} Task: {task_id}.{suffix}"
