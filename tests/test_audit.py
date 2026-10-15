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


_WRITER = """
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from proxmox_mcp import audit
for i in range(int(sys.argv[4])):
    audit.append(Path(sys.argv[2]), {"ts": f"{sys.argv[3]}-{i}", "cmd": "x" * 2000, "rc": 0})
"""


def test_concurrent_processes_keep_chain_intact():
    """Several MCP processes share one log (AHMETPC runs ~10). Without a lock
    two of them read the same tail hash and the second line's `prev` breaks
    the chain (the CT 207 break of 2026-10-08)."""
    import subprocess
    os.environ.pop("PROXMOX_AUDIT_HMAC_KEY", None)
    p = _tmp()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    procs, per = 6, 60
    ps = [subprocess.Popen([sys.executable, "-c", _WRITER, root, str(p), str(n), str(per)])
          for n in range(procs)]
    assert all(x.wait(timeout=120) == 0 for x in ps)
    r = audit.verify_chain(p)
    assert r["chained"] == procs * per, r
    assert r["ok"], r


_SECRET = "Zq8fake-Secret-77xYw"


def _polluted(p: Path, cmds=("whoami", "cat .env", "ls", "df"), key=None) -> list[dict]:
    """Chain written before redaction existed: `cat .env` leaks a token."""
    rows, prev = [], ""
    for c in cmds:
        e = {"ts": c, "cmd": c, "rc": 0, "prev": prev,
             "stdout_preview": f"N8N_TOKEN={_SECRET}" if c == "cat .env" else "ok"}
        e["hash"] = prev = audit._digest(e, key)
        rows.append(e)
    p.write_text("".join(json.dumps(e) + "\n" for e in rows), encoding="utf-8")
    return rows


def _refused(p: Path, match: str, **kw) -> bool:
    try:
        audit.rewrite_redacted(p, **kw)
    except RuntimeError as exc:
        return match in str(exc)
    return False


def test_rewrite_intact_log_rebuilt_as_before():
    """madde 262: an intact log rewrites exactly as before — no flag needed."""
    os.environ.pop("PROXMOX_AUDIT_HMAC_KEY", None)
    p = _tmp()
    _polluted(p)
    r = audit.rewrite_redacted(p)
    assert _SECRET not in p.read_text(encoding="utf-8")
    assert r["mismatched"] == [] and r["redacted"] == 1, r
    assert r["verify"]["ok"] and r["verify"]["chained"] == 5, r  # 4 + marker
    before = p.read_bytes()
    assert audit.rewrite_redacted(p)["rewritten"] == 0 and p.read_bytes() == before


def test_rewrite_does_not_launder_tampered_line():
    """madde 262: rewrite re-signed a line whose hash no longer matched, so a
    tampered line came out verifying under the 'redaction' cover."""
    os.environ.pop("PROXMOX_AUDIT_HMAC_KEY", None)
    p = _tmp()
    rows = _polluted(p)
    lines = p.read_text(encoding="utf-8").splitlines()
    forged = dict(rows[1], cmd="cat .env; curl evil")  # stored hash kept
    lines[1] = json.dumps(forged)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    before = p.read_bytes()
    assert _refused(p, "fail their hash"), "tampered log rewritten without force"
    assert p.read_bytes() == before
    r = audit.rewrite_redacted(p, force=True)
    assert r["mismatched"] == [2], r
    out = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    assert _SECRET not in json.dumps(out)              # still masked
    assert out[1]["hash"] == rows[1]["hash"] and out[1]["cmd"] == forged["cmd"]
    assert out[2]["prev"] == rows[1]["hash"]            # successor not re-pointed
    v = r["verify"]
    assert not v["ok"] and v["first_break"] == {"line": 2, "why": "hash mismatch (tampered, or wrong/missing key)"}, v
    assert "kept broken" in out[-1]["note"]


def test_rewrite_keeps_prev_break():
    """A deleted line (prev break, hashes valid) still rewrites without force
    and stays broken at the same place."""
    os.environ.pop("PROXMOX_AUDIT_HMAC_KEY", None)
    p = _tmp()
    _polluted(p)
    lines = p.read_text(encoding="utf-8").splitlines()
    del lines[2]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    was = audit.verify_chain(p)["first_break"]
    assert was["line"] == 3 and "prev mismatch" in was["why"], was
    r = audit.rewrite_redacted(p)
    assert r["redacted"] == 1 and r["mismatched"] == [], r
    assert r["verify"]["first_break"] == was, r
    assert _SECRET not in p.read_text(encoding="utf-8")


def test_rewrite_refuses_wrong_key_even_forced():
    """Keyless run over an HMAC log: every line 'mismatches'. Re-signing would
    strip the HMAC; force must not cover that."""
    os.environ.pop("PROXMOX_AUDIT_HMAC_KEY", None)
    p = _tmp()
    _polluted(p, key=b"k1")
    before = p.read_bytes()
    assert _refused(p, "wrong or not loaded", force=True)
    assert p.read_bytes() == before
    os.environ["PROXMOX_AUDIT_HMAC_KEY"] = "k1"
    try:
        r = audit.rewrite_redacted(p)
        assert r["verify"]["ok"] and r["verify"]["hmac"] and r["redacted"] == 1, r
    finally:
        os.environ.pop("PROXMOX_AUDIT_HMAC_KEY", None)


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
