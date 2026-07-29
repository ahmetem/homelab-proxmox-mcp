"""Operator approval for destructive tools — the human-side half of the gate.

`confirm=true` is a tool *argument*, so it is filled in by the model. It proves
the model's intent and nothing about the operator's: nothing in the protocol
forces the question to reach a human. This module routes the same decision to the
human through the client, using the resolver injection added in MCP spec revision
2026-07-28 (mcp SDK >= 2.0):

  * The approval arrives in a parameter filled by a resolver, not from the
    tool-call arguments, so it never appears in the model-facing input schema and
    cannot be supplied — or forged — by a tool call.
  * On the 2026-07-28 wire the question travels as an `InputRequiredResult`
    (multi round-trip request); the SDK also serves it over the older
    server-to-client `elicitation/create` when the client speaks an earlier
    revision.

It degrades to the confirm flag alone — exactly today's behaviour — when the SDK
is 1.x or the client does not offer form elicitation, so a client without
human-in-the-loop support keeps working instead of losing every destructive tool.
The question is only asked once `confirm=true` is already set, so a missing flag
is still refused on the spot without bothering the operator.

Usage — apply *under* the `@mcp.tool(...)` decorator and accept the parameter:

    @mcp.tool(name="proxmox_snapshot", ...)
    @gated("proxmox_snapshot", "the snapshot and its unique data are gone")
    async def proxmox_snapshot(params: SnapshotManageInput, operator_ack=None):
        ...
        if not params.confirm:
            return missing_confirm(...)
        refused = ack_refusal(operator_ack, "proxmox_snapshot")
        if refused:
            return refused
"""
from __future__ import annotations

import inspect
import typing
from typing import Annotated, Any, Callable, Optional

from pydantic import BaseModel, Field

try:  # mcp >= 2.0
    from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
    from mcp.server.mcpserver import Context, Resolve
    from mcp.server.mcpserver.resolve import Elicit

    HAVE_RESOLVE = True
except ImportError:  # mcp 1.x maintenance line: no resolver injection
    HAVE_RESOLVE = False

ACK_PARAM = "operator_ack"

# Identifying fields worth showing the human, in the order they read best. Only
# the ones a given input model actually carries are rendered.
_TARGET_FIELDS = (
    "node", "vmid", "vm_type", "name", "hostname", "snapname", "storage",
    "pool", "dataset", "device", "vg", "lv", "target", "action", "command",
)


_OUTCOME_PHRASE = {"decline": "was declined", "cancel": "was cancelled"}


class OperatorAck(BaseModel):
    """The single question put to the human."""

    approve: bool = Field(
        description="Approve this operation on the live homelab? No = refuse."
    )


def _has_form_elicitation(ctx: Any) -> bool:
    """True when the client can render a form question.

    Mirrors the SDK's own capability rule: a bare `elicitation: {}` predates
    elicitation modes and counts as form support, url-only does not.
    """
    capabilities = getattr(ctx, "client_capabilities", None)
    elicitation = getattr(capabilities, "elicitation", None)
    if elicitation is None:
        return False
    return elicitation.form is not None or elicitation.url is None


def _question(label: str, params: Any, consequence: str) -> str:
    target = "\n".join(
        f"  {field}: {getattr(params, field)}"
        for field in _TARGET_FIELDS
        if getattr(params, field, None) is not None
    )
    return (
        f"Approve `{label}` on the live homelab?\n\n"
        f"{target}\n\n"
        f"Consequence: {consequence}"
    )


def _ask_operator(label: str, consequence: str) -> Callable[..., Any]:
    """Build the resolver that asks the human for one tool.

    Returns None (no question) when the model has not set `confirm` yet, or when
    the client has no channel to ask a human — both leave the decision to the
    flag-based gate in the tool body.
    """

    def ask_operator(params: Any, ctx: "Context") -> "Elicit[OperatorAck] | None":
        if not getattr(params, "confirm", False):
            return None
        if not _has_form_elicitation(ctx):
            return None
        return Elicit(_question(label, params, consequence), OperatorAck)

    return ask_operator


def _hide_param(fn: Any) -> Any:
    """Drop the ack parameter from what the SDK introspects (mcp 1.x path).

    Without this the parameter would land in the model-facing input schema as an
    ordinary argument — the exact thing this module exists to avoid.

    The annotations are resolved here rather than left as the strings that
    `from __future__ import annotations` produces: once `__signature__` is set,
    `inspect.signature()` returns it verbatim and never evaluates string
    annotations, so the SDK would try to build its argument model from the name
    `"SnapshotManageInput"` and fail with an undefined-type error.
    """
    signature = inspect.signature(fn)
    if ACK_PARAM not in signature.parameters:
        return fn
    hints = typing.get_type_hints(fn, include_extras=True)
    kept = [
        param.replace(annotation=hints.get(name, param.annotation))
        for name, param in signature.parameters.items()
        if name != ACK_PARAM
    ]
    fn.__signature__ = signature.replace(
        parameters=kept,
        return_annotation=hints.get("return", signature.return_annotation),
    )
    fn.__annotations__.pop(ACK_PARAM, None)
    return fn


def gated(label: str, consequence: str) -> Callable[[Any], Any]:
    """Wire a destructive tool's ack parameter to a human, when one is reachable.

    `label` names the operation in the question, `consequence` is the one-line
    "what you lose" the human is shown. Apply under `@mcp.tool(...)`.
    """

    def decorate(fn: Any) -> Any:
        if not HAVE_RESOLVE:
            return _hide_param(fn)
        fn.__annotations__[ACK_PARAM] = Annotated[
            ElicitationResult[OperatorAck], Resolve(_ask_operator(label, consequence))
        ]
        return fn

    return decorate


def ack_refusal(ack: Any, action: str) -> Optional[str]:
    """None when the operation may proceed, else the refusal to return verbatim.

    Proceeding covers the degraded cases on purpose: `ack` is None on mcp 1.x,
    and the resolver hands back an accepted-but-empty outcome when it chose not
    to ask (no human channel). Both mean "no human verdict available" — the
    confirm flag the caller already checked is the whole gate there.
    """
    if not HAVE_RESOLVE or ack is None:
        return None
    if isinstance(ack, AcceptedElicitation):
        verdict = ack.data
        if verdict is None:
            return None
        if verdict.approve:
            return None
        return f"Refused: the operator was asked to approve '{action}' and declined."
    phrase = _OUTCOME_PHRASE.get(getattr(ack, "action", None), "went unanswered")
    return (
        f"Refused: '{action}' needs the operator's approval and the request "
        f"{phrase}. Nothing was changed."
    )
