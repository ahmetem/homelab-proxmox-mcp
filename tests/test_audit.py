"""Tests for the hash-chained, tamper-evident audit log (proxmox_mcp.audit).

Covers: an intact chain (with a legacy pre-chain line), tamper detection,
line-deletion detection, HMAC keyed mode + wrong-key rejection, and the
missing-file case. Runnable with pytest or standalone.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proxmox_mcp import audit  # noqa: E402


def _tmp() -> Path:
    return Path(tempfile.mkdtemp()) / "audit.log"


def test_chain_intact_with_legacy_prefix():
    os.environ.pop("PROXMOX_AUDIT_HMAC_KEY", None)
    p = _tmp()
    p.write_text(json.dumps({"ts": "old", "cmd": "legacy"}) + "\n", encoding="utf-8")
    for c in ("whoami", "ls", "df"):
        audit.append(p, {"ts": c, "cmd": c, "rc": 0})
    r = audit.verify_chain(p)
    assert r["ok"] and r["chained"] == 3 and r["legacy"] == 1 and r["hmac"] is False


def test_tamper_detected():
    os.environ.pop("PROXMOX_AUDIT_HMAC_KEY", None)
    p = _tmp()
    for c in ("a", "b", "c"):
        audit.append(p, {"ts": c, "cmd": c, "rc": 0})
    lines = p.read_text(encoding="utf-8").splitlines()
    obj = json.loads(lines[1])
    obj["cmd"] = "rm -rf /"
    lines[1] = json.dumps(obj, ensure_ascii=False)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = audit.verify_chain(p)
    assert r["ok"] is False and r["first_break"]["line"] == 2


def test_delete_detected():
    os.environ.pop("PROXMOX_AUDIT_HMAC_KEY", None)
    p = _tmp()
    for c in ("one", "two", "three"):
        audit.append(p, {"ts": c, "cmd": c, "rc": 0})
    lines = p.read_text(encoding="utf-8").splitlines()
    del lines[1]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = audit.verify_chain(p)
    assert r["ok"] is False


def test_leading_deletion_detected():
    os.environ.pop("PROXMOX_AUDIT_HMAC_KEY", None)
    p = _tmp()
    for c in ("one", "two", "three"):
        audit.append(p, {"ts": c, "cmd": c, "rc": 0})
    lines = p.read_text(encoding="utf-8").splitlines()
    del lines[0]  # delete the genesis line -> chain must no longer verify
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = audit.verify_chain(p)
    assert r["ok"] is False


def test_hmac_mode_and_wrong_key():
    os.environ["PROXMOX_AUDIT_HMAC_KEY"] = "supersecretkey"
    p = _tmp()
    audit.append(p, {"ts": "k1", "cmd": "x", "rc": 0})
    audit.append(p, {"ts": "k2", "cmd": "y", "rc": 0})
    r = audit.verify_chain(p)
    assert r["ok"] and r["hmac"] is True and r["chained"] == 2
    os.environ["PROXMOX_AUDIT_HMAC_KEY"] = "wrongkey"
    assert audit.verify_chain(p)["ok"] is False
    os.environ.pop("PROXMOX_AUDIT_HMAC_KEY", None)


def test_missing_file_is_ok():
    os.environ.pop("PROXMOX_AUDIT_HMAC_KEY", None)
    r = audit.verify_chain(Path(tempfile.mkdtemp()) / "nope.log")
    assert r["exists"] is False and r["ok"] is True and r["chained"] == 0


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
