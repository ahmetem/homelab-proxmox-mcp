"""Operator-approval gate tests (proxmox_mcp.operator_ack).

The gate's whole point is that the approval cannot come from the model, so the
first test — the ack parameter never appears in the model-facing input schema —
runs on both SDK eras. The elicitation behaviour itself needs mcp >= 2.0 and is
skipped on the 1.x maintenance line, where the gate deliberately degrades to the
confirm flag alone.

Same conventions as test_safety_gates.py: config is stubbed, the mutating HTTP
verb is spied on so a refusal that still called out would fail, and the file is
runnable with pytest or standalone.
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import proxmox_mcp.config as _cfg  # noqa: E402

_cfg.PROXMOX_HOST = _cfg.PROXMOX_HOST or "192.0.2.1"
_cfg.PROXMOX_USER = _cfg.PROXMOX_USER or "root@pam"
_cfg.PROXMOX_TOKEN_NAME = _cfg.PROXMOX_TOKEN_NAME or "t"
_cfg.PROXMOX_TOKEN_VALUE = _cfg.PROXMOX_TOKEN_VALUE or "x"

from proxmox_mcp import http_client  # noqa: E402
from proxmox_mcp import operator_ack as oa  # noqa: E402
from proxmox_mcp.mcp_instance import mcp  # noqa: E402
from proxmox_mcp.models import SnapshotManageInput  # noqa: E402
from proxmox_mcp.tools.snapshots import proxmox_snapshot  # noqa: E402

V2 = oa.HAVE_RESOLVE

if V2:
    from mcp.server.elicitation import (  # noqa: E402
        AcceptedElicitation,
        CancelledElicitation,
        DeclinedElicitation,
    )
    from mcp.server.mcpserver.resolve import Elicit  # noqa: E402


def _skip(reason: str) -> None:
    """Skip under pytest, no-op standalone (mirrors the plain-function style here)."""
    try:
        import pytest

        pytest.skip(reason)
    except ImportError:
        pass


def _ctx(elicitation) -> object:
    """A Context stand-in carrying only what the resolver reads."""
    caps = types.SimpleNamespace(elicitation=elicitation)
    return types.SimpleNamespace(client_capabilities=caps)


_FORM = types.SimpleNamespace(form=object(), url=None)
_URL_ONLY = types.SimpleNamespace(form=None, url=object())

_DELETE = dict(
    node="pve", vmid=101, vm_type="qemu", action="delete", snapname="testsnap"
)


class _DeleteSpy:
    def __init__(self) -> None:
        self.calls = 0

    async def delete(self, path, params=None):  # matches http_client.delete
        self.calls += 1
        return "UPID:spy"


def _patch_delete():
    spy = _DeleteSpy()
    orig = http_client.delete
    http_client.delete = spy.delete
    return spy, orig


async def _no_wait(node, task_id, seconds):  # stub http_client.wait_for_task
    return ""


# ---------- the security property: holds on both SDK eras ---------------------

def test_ack_param_is_not_in_the_model_facing_schema():
    tool = next(
        t for t in asyncio.run(mcp.list_tools()) if t.name == "proxmox_snapshot"
    )
    # v2 renamed the field to snake_case; this file has to read both.
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema")
    assert list(schema.get("properties", {})) == ["params"]
    assert oa.ACK_PARAM not in str(schema)


def test_no_human_verdict_means_the_confirm_flag_is_the_whole_gate():
    # mcp 1.x, or a client with no elicitation: the tool must stay usable.
    assert oa.ack_refusal(None, "proxmox_snapshot") is None


# ---------- resolver: when is the human actually asked? (mcp >= 2.0) ----------

def test_resolver_stays_silent_until_the_model_sets_confirm():
    if not V2:
        return _skip("resolver injection needs mcp >= 2.0")
    ask = oa._ask_operator("proxmox_snapshot", "permanent")
    params = SnapshotManageInput(**_DELETE)  # confirm defaults to False
    assert ask(params, _ctx(_FORM)) is None


def test_resolver_stays_silent_without_a_form_elicitation_channel():
    if not V2:
        return _skip("resolver injection needs mcp >= 2.0")
    ask = oa._ask_operator("proxmox_snapshot", "permanent")
    params = SnapshotManageInput(**_DELETE, confirm=True, i_understand_data_loss=True)
    assert ask(params, _ctx(None)) is None
    assert ask(params, _ctx(_URL_ONLY)) is None  # url-only is not a form channel


def test_resolver_asks_and_names_the_target_and_consequence():
    if not V2:
        return _skip("resolver injection needs mcp >= 2.0")
    ask = oa._ask_operator("proxmox_snapshot", "delete is permanent")
    params = SnapshotManageInput(**_DELETE, confirm=True, i_understand_data_loss=True)
    marker = ask(params, _ctx(_FORM))
    assert isinstance(marker, Elicit)
    assert marker.schema is oa.OperatorAck
    for expected in ("proxmox_snapshot", "101", "testsnap", "delete is permanent"):
        assert expected in marker.message


# ---------- verdict handling (mcp >= 2.0) ------------------------------------

def test_refusal_text_for_every_negative_verdict():
    if not V2:
        return _skip("elicitation outcomes need mcp >= 2.0")
    said_no = AcceptedElicitation(data=oa.OperatorAck(approve=False))
    assert "declined" in oa.ack_refusal(said_no, "proxmox_snapshot")
    for outcome in (DeclinedElicitation(), CancelledElicitation()):
        refusal = oa.ack_refusal(outcome, "proxmox_snapshot")
        assert "Nothing was changed" in refusal


def test_approval_lets_the_call_through():
    if not V2:
        return _skip("elicitation outcomes need mcp >= 2.0")
    said_yes = AcceptedElicitation(data=oa.OperatorAck(approve=True))
    assert oa.ack_refusal(said_yes, "proxmox_snapshot") is None
    # An accepted-but-empty outcome is the resolver declining to ask. Built the
    # way the SDK builds it: a plain resolver return skips validation, and
    # `data` is typed as a BaseModel so the normal constructor would reject None.
    empty = AcceptedElicitation[Any].model_construct(data=None)
    assert oa.ack_refusal(empty, "proxmox_snapshot") is None


# ---------- end to end through the tool body ---------------------------------

def test_operator_refusal_makes_no_delete_call():
    if not V2:
        return _skip("elicitation outcomes need mcp >= 2.0")

    async def _t():
        spy, orig = _patch_delete()
        try:
            result = await proxmox_snapshot(
                SnapshotManageInput(
                    **_DELETE, confirm=True, i_understand_data_loss=True
                ),
                operator_ack=AcceptedElicitation(data=oa.OperatorAck(approve=False)),
            )
        finally:
            http_client.delete = orig
        assert "declined" in result
        assert spy.calls == 0  # the model said yes; the human said no; nothing ran

    asyncio.run(_t())


def test_operator_approval_reaches_the_api():
    if not V2:
        return _skip("elicitation outcomes need mcp >= 2.0")

    async def _t():
        spy, orig = _patch_delete()
        wait_orig = http_client.wait_for_task
        http_client.wait_for_task = _no_wait
        try:
            result = await proxmox_snapshot(
                SnapshotManageInput(
                    **_DELETE, confirm=True, i_understand_data_loss=True
                ),
                operator_ack=AcceptedElicitation(data=oa.OperatorAck(approve=True)),
            )
        finally:
            http_client.delete = orig
            http_client.wait_for_task = wait_orig
        assert "OK:" in result
        assert spy.calls == 1

    asyncio.run(_t())


def test_data_loss_flag_is_still_checked_before_the_human_is_asked():
    async def _t():
        spy, orig = _patch_delete()
        try:
            result = await proxmox_snapshot(
                SnapshotManageInput(**_DELETE, confirm=True)  # no data-loss ack
            )
        finally:
            http_client.delete = orig
        assert "i_understand_data_loss=true" in result
        assert spy.calls == 0

    asyncio.run(_t())


def _run_standalone():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_standalone())
