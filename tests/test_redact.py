"""Tests for audit-log secret redaction (proxmox_mcp.redact + audit wiring).

All secrets below are fake. Covers: env literals under neutral names, the
pattern layer on the shapes seen in the real logs (KEY=value lines, DSNs,
credential URLs, tokens, PEM keys), names that must NOT be masked, redaction
before hashing (chain stays intact), the 0600 file mode, and the one-off
rewrite of an already-polluted log.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proxmox_mcp import audit, host_ssh  # noqa: E402
from proxmox_mcp.redact import redact  # noqa: E402

FAKE = "Zq8fake-Secret-77xYw"


def _tmp() -> Path:
    return Path(tempfile.mkdtemp()) / "audit.log"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("PROXMOX_AUDIT_HMAC_KEY", raising=False)


def test_env_literal_under_neutral_name(monkeypatch):
    # madde 227: CT209_PG_HA / CT202_N8N_DB carry no PASS/TOKEN in the name.
    monkeypatch.setenv("CT209_PG_HA", FAKE)
    out = redact(f"psql -c 'select 1' # {FAKE} and again {FAKE}")
    assert FAKE not in out and out.count("<gizli:CT209_PG_HA>") == 2


def test_env_dsn_masks_only_password(monkeypatch):
    monkeypatch.setenv("OLAY_DSN", f"postgresql://agent:{FAKE}@192.168.1.25:5432/db")
    assert redact(f"echo {FAKE}") == "echo <gizli:OLAY_DSN>"


def test_env_non_secrets_left_alone(monkeypatch):
    monkeypatch.setenv("PROXMOX_SSH_KEY_PATH", "/root/.ssh/id_ed25519")
    monkeypatch.setenv("PROXMOX_SSH_HOST", "192.168.1.21")
    monkeypatch.setenv("PROXMOX_TOKEN_NAME", "mcp-server-long")
    monkeypatch.setenv("SMTP_TO", "someone@example.com")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-desktop")
    cmd = ("ssh -i /root/.ssh/id_ed25519 root@192.168.1.21 mcp-server-long someone@example.com"
           " claude-desktop")
    assert redact(cmd) == cmd


@pytest.mark.parametrize("text,leak", [
    (f"PBS_TOKEN_SECRET={FAKE}\nNEXT=1", FAKE),                       # cat .env
    (f"22:TELEGRAM_BOT_TOKEN={FAKE}", FAKE),                          # grep -n
    (f"DB_POSTGRESDB_PASSWORD='{FAKE}'", FAKE),
    (f'user="x"; pw="{FAKE} with space"', FAKE),
    (f'{{"password": "{FAKE}"}}', FAKE),
    (f"PGPASSWORD={FAKE} psql -h db", FAKE),
    (f"DATABASE_URL=postgresql://ha:{FAKE}@10.0.0.5:5432/ha", FAKE),
    (f"git remote -v https://x-access:{FAKE}@github.com/a/b", FAKE),
    (f"curl -H 'Authorization: Bearer {FAKE}' http://x", FAKE),
    (f"Authorization: PBSAPIToken=root@pam!mcp:{FAKE}", FAKE),
    (f"PVEAPIToken=root@pam!mcp={FAKE}", FAKE),
    (f"tool --password {FAKE} --other", FAKE),
    (f"GITHUB_PAT_GH_CLI={FAKE}", FAKE),
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk" + FAKE, FAKE),  # truncated preview
    ("T='sk-ant-oat01-" + FAKE + "abcdefgh'", FAKE),
    ("echo 123456789:AA" + "x" * 13 + FAKE + " ok", FAKE),
])
def test_patterns(text, leak):
    out = redact(text)
    assert leak not in out and "<gizli" in out


@pytest.mark.parametrize("text", [
    "ssh -o StrictHostKeyChecking=no root@host",
    "PROXMOX_TOKEN_NAME=mcp-server",
    "PROXMOX_SSH_KEY_PATH=/root/.ssh/id",
    "PATH=/usr/bin:/bin",
    'PGPASSWORD="$PW" psql',
    "pveum user token list root@pam",
    "zfs list -o name,used vmpool",
    "VISION_MAX_TOKENS=4096",
    "AUTHELIA_SESSION_SECRET_FILE=/secrets/session",
    "PWD=/root",
    "homelab ALL=(ALL) NOPASSWD: ALL",
])
def test_non_secrets_untouched(text):
    assert redact(text) == text


def test_base64_blob_with_secret_masked_whole():
    import base64
    blob = base64.b64encode(f"HA_WS=wss://h\nHA_TOKEN={FAKE}\n".encode()).decode()
    out = redact(f"echo '{blob}' | base64 -d > .env")
    assert blob[:30] not in out and "<gizli:base64>" in out
    harmless = base64.b64encode(b"#!/bin/sh\necho hello world\n").decode()
    assert redact(f"echo {harmless}") == f"echo {harmless}"


def test_kv_does_not_jump_lines():
    text = "GOOGLE_REFRESH_TOKEN=\n\nnext line stays"
    assert redact(text) == text


def test_idempotent(monkeypatch):
    monkeypatch.setenv("CT202_N8N_DB", FAKE)
    once = redact(f"N8N_DB_PASS={FAKE} postgresql://u:{FAKE}@h/db")
    assert redact(once) == once


def test_append_redacts_before_hashing(monkeypatch):
    monkeypatch.setenv("SOME_SECRET", FAKE)
    p = _tmp()
    audit.append(p, {"cmd": f"echo {FAKE}", "stdout_preview": f"X_TOKEN={FAKE}",
                     "stderr_preview": "", "note": FAKE, "rc": 0})
    raw = p.read_text(encoding="utf-8")
    assert FAKE not in raw
    assert audit.verify_chain(p)["ok"]


def test_audit_log_redacts_before_truncation(monkeypatch):
    # A secret straddling the 500-char cut must not leave a recognisable prefix.
    monkeypatch.setenv("SOME_SECRET", FAKE)
    p = _tmp()
    monkeypatch.setattr(host_ssh, "_AUDIT_PATH", p)
    host_ssh.audit_log("x" * 490 + FAKE, 0)
    assert FAKE[:10] not in p.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_log_created_0600_and_healed():
    p = _tmp()
    audit.append(p, {"cmd": "a"})
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    os.chmod(p, 0o644)
    audit.append(p, {"cmd": "b"})
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def _write_polluted(p: Path) -> None:
    """Old-style log: legacy line + chained lines written without redaction."""
    lines = [json.dumps({"ts": "old", "cmd": f"PGPASSWORD={FAKE} psql"})]
    prev = ""
    for cmd, out in (("whoami", "root"), ("cat .env", f"N8N_TOKEN={FAKE}"), ("ls", "a b")):
        e = {"ts": cmd, "cmd": cmd, "rc": 0, "stdout_preview": out, "prev": prev}
        e["hash"] = audit._digest(e, None)
        prev = e["hash"]
        lines.append(json.dumps(e, ensure_ascii=False))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_rewrite_redacted_rebuilds_chain():
    p = _tmp()
    _write_polluted(p)
    assert audit.verify_chain(p)["ok"]
    r = audit.rewrite_redacted(p, note="test")
    text = p.read_text(encoding="utf-8")
    assert FAKE not in text
    assert r["redacted"] == 2 and r["counts"] == {"<gizli>": 2}
    assert r["verify"]["ok"] and r["verify"]["legacy"] == 1
    assert r["verify"]["chained"] == 4  # 3 rewritten + the marker entry
    rows = [json.loads(x) for x in text.splitlines()]
    assert [x["ts"] for x in rows[:4]] == ["old", "whoami", "cat .env", "ls"]
    assert "redaction rewrite" in rows[-1]["note"] and "test" in rows[-1]["note"]
    # A clean log is left byte-for-byte alone (no marker either).
    before = p.read_bytes()
    assert audit.rewrite_redacted(p)["rewritten"] == 0 and p.read_bytes() == before
