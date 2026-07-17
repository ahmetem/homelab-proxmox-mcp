"""Tamper-evident, hash-chained audit log shared by the SSH exec tools.

Each appended entry carries `prev` (the previous chained entry's hash) and
`hash` — SHA-256 by default, or HMAC-SHA256 when PROXMOX_AUDIT_HMAC_KEY is set
— computed over the entry's canonical JSON (which includes `prev`). This links
entries into a chain: altering, deleting or reordering any line changes every
hash after it, which verify_chain() detects.

Without a key the chain is *integrity-evident*: it catches accidental
corruption, truncation and deletion. With a key it is *tamper-evident*: an
attacker who cannot read the key cannot forge a valid chain. Logging never
raises — a logging failure must never break the operation being logged.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any, Optional


def _key() -> Optional[bytes]:
    k = os.getenv("PROXMOX_AUDIT_HMAC_KEY", "").strip()
    return k.encode("utf-8") if k else None


def _digest(entry_without_hash: dict, key: Optional[bytes]) -> str:
    """Deterministic digest over the entry's canonical JSON (keys sorted, no
    whitespace). `prev` is part of the entry, so it is folded into the chain."""
    canon = json.dumps(
        entry_without_hash, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, default=str,
    ).encode("utf-8")
    if key:
        return hmac.new(key, canon, hashlib.sha256).hexdigest()
    return hashlib.sha256(canon).hexdigest()


def _last_hash(path: Path) -> str:
    """Hash of the most recent chained entry, or '' if the file is absent,
    empty, or ends in a legacy/unparseable line. Reads only the file tail."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return ""
            block = min(size, 65536)
            f.seek(size - block)
            data = f.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return ""
    except Exception:
        return ""
    for line in reversed(data.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            return ""  # unparseable tail -> start a fresh chain from here
        h = obj.get("hash") if isinstance(obj, dict) else None
        return h if isinstance(h, str) and h else ""
    return ""


def append(path: Path, entry: dict) -> None:
    """Append `entry` as one hash-chained JSON line. Never raises."""
    try:
        base = dict(entry)
        base["prev"] = _last_hash(path)
        base["hash"] = _digest(base, _key())
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(base, ensure_ascii=False) + "\n")
    except Exception:
        pass  # never let logging break a real call


def read_entries(path: Path, limit: int = 20) -> list[dict]:
    """Return up to the last `limit` parsed entries, oldest-to-newest.

    Read-only, never raises. Blank/unparseable lines are skipped; legacy lines
    (no `hash`) are included. `limit<=0` returns every entry. Used by the audit
    replay path in proxmox_audit_verify — it does not verify the chain (that is
    verify_chain's job), it just surfaces what was recorded."""
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except FileNotFoundError:
        return []
    except Exception:
        return []
    return out[-limit:] if limit and limit > 0 else out


def verify_chain(path: Path) -> dict:
    """Verify the hash chain in `path`. Returns a summary; never raises.

    Keys: exists, total, chained, legacy, ok, hmac, first_break. Legacy
    (pre-chain, no `hash`) lines are counted but do not break the chain that
    follows them.
    """
    result: dict[str, Any] = {
        "exists": path.exists(), "total": 0, "chained": 0, "legacy": 0,
        "ok": True, "hmac": _key() is not None, "first_break": None,
    }
    if not result["exists"]:
        return result

    key = _key()
    prev_expected: Optional[str] = None  # None until the first chained line

    def _break(line_no: int, why: str) -> None:
        result["ok"] = False
        if result["first_break"] is None:
            result["first_break"] = {"line": line_no, "why": why}

    try:
        with path.open("r", encoding="utf-8") as f:
            for i, raw in enumerate(f, 1):
                line = raw.strip()
                if not line:
                    continue
                result["total"] += 1
                try:
                    obj = json.loads(line)
                except ValueError:
                    _break(i, "invalid JSON")
                    continue
                if not isinstance(obj, dict) or "hash" not in obj:
                    result["legacy"] += 1
                    continue
                stored = obj.get("hash")
                recomputed = _digest(
                    {k: v for k, v in obj.items() if k != "hash"}, key
                )
                if recomputed != stored:
                    _break(i, "hash mismatch (tampered, or wrong/missing key)")
                elif prev_expected is None:
                    # First chained line must be genesis (prev == ""); a
                    # non-empty prev here means the leading line(s) were deleted.
                    if obj.get("prev"):
                        _break(i, "chain break (leading line(s) deleted — missing genesis)")
                elif obj.get("prev") != prev_expected:
                    _break(i, "chain break (prev mismatch — line deleted or reordered)")
                result["chained"] += 1
                prev_expected = stored
    except Exception as exc:  # pragma: no cover
        _break(0, f"read error: {type(exc).__name__}")
    return result
