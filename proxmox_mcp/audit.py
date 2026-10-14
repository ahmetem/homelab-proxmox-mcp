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

Every string field is passed through proxmox_mcp.redact before hashing, so the
chain covers the masked text. The file is created 0600 (it records commands
and output previews from the host). Appends from concurrent processes are
serialised by an exclusive file lock (flock / msvcrt), so the chain survives
several MCP servers writing the same log.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from proxmox_mcp.redact import redact, redact_counted


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


# Windows byte-range locks are mandatory: locking real log bytes would make
# other processes' reads (_last_hash, verify_chain) fail. One byte far past any
# realistic EOF is a pure mutex.
_LOCK_OFFSET = 1 << 40


def _lock(fd: int) -> bool:
    """Exclusive cross-process lock on an open log fd; False if not obtained."""
    try:
        if os.name == "nt":
            import msvcrt
            import time
            deadline = time.monotonic() + 10
            while True:
                os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    return True
                except OSError:
                    if time.monotonic() > deadline:
                        return False
                    time.sleep(0.005)  # LK_LOCK retries once a second: too coarse
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        return True
    except Exception:
        return False


def _heal_mode(fd: int) -> None:
    """Logs created before 1.7.0 were 0644. Strip world bits; strip group bits
    too unless the log is group-writable on purpose (CT 215: the sage core and
    its agent uid share it). Not the owner -> leave it, still write."""
    if not hasattr(os, "fchmod"):
        return
    try:
        mode = os.fstat(fd).st_mode & 0o777
        healed = mode & ~0o007 if mode & 0o020 else mode & ~0o077
        if healed != mode:
            os.fchmod(fd, healed)
    except OSError:
        pass


def append(path: Path, entry: dict) -> None:
    """Append `entry` as one hash-chained JSON line. Never raises.

    Several MCP processes share one log, so reading the tail hash and writing
    the new line happen under an exclusive lock; otherwise two writers chain
    to the same `prev` and the chain breaks. If the lock can't be had within
    ~10 s the line is written anyway: a visible break beats a lost entry."""
    try:
        base = {k: redact(v) if isinstance(v, str) else v for k, v in entry.items()}
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            locked = _lock(fd)
            _heal_mode(fd)
            base["prev"] = _last_hash(path)
            base["hash"] = _digest(base, _key())
            os.write(fd, (json.dumps(base, ensure_ascii=False) + "\n").encode("utf-8"))
            if locked and os.name == "nt":
                import msvcrt
                os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)  # also releases the POSIX flock
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


def rewrite_redacted(path: Path, note: str = "") -> dict:
    """Redact secrets already sitting in `path` and rebuild the hash chain.

    One-off maintenance for logs written before redaction existed. Every line
    keeps its position and fields; string fields go through the same redactor
    as append(). Chained lines get a recomputed `hash`, and each `prev` is
    mapped from the old hash to the new one, so an intact chain stays intact
    and a pre-existing break stays visible. Legacy lines stay unchained. When
    anything changed, the file is replaced atomically (0600) and a marker
    entry carrying `note` is appended. The redaction is not undoable: the
    original bytes are not kept anywhere.

    Returns {lines, redacted, rewritten, unparseable, counts: {label: n}, verify}.
    """
    counts: Counter = Counter()
    key = _key()
    for _attempt in range(3):
        raw = path.read_bytes()
        out_lines: list[str] = []
        new_of: dict[str, str] = {}
        changed = redacted = unparseable = 0
        for line in raw.decode("utf-8", "replace").splitlines():
            try:
                obj = json.loads(line) if line.strip() else None
            except ValueError:
                obj = None
            if not isinstance(obj, dict):
                if line.strip():
                    unparseable += 1
                out_lines.append(line)
                continue
            red = {k: redact_counted(v, counts) if isinstance(v, str) and k not in ("hash", "prev") else v
                   for k, v in obj.items()}
            if any(red[k] != obj[k] for k in obj if k not in ("hash", "prev")):
                redacted += 1
            if "hash" in obj:
                old = obj["hash"]
                red["prev"] = new_of.get(obj.get("prev") or "", obj.get("prev") or "")
                red.pop("hash")
                red["hash"] = _digest(red, key)
                new_of[old] = red["hash"]
            if red != obj:
                changed += 1
                out_lines.append(json.dumps(red, ensure_ascii=False))
            else:
                out_lines.append(line)
        if not changed:
            return {"lines": len(out_lines), "redacted": 0, "rewritten": 0,
                    "unparseable": unparseable, "counts": {}, "verify": verify_chain(path)}
        tmp = path.with_name(path.name + ".redact-tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(out_lines) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if path.stat().st_size != len(raw):  # an append raced us; redo
            tmp.unlink()
            counts.clear()
            continue
        os.replace(tmp, path)
        break
    else:
        raise RuntimeError("log kept growing during rewrite; retry when idle")
    passes = sum(counts.values())
    append(path, {"ts": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "rc": None, "cmd": "",
                  "note": f"redaction rewrite: {redacted} lines masked ({passes} passes), "
                          f"{changed} rewritten, chain rebuilt" + (f"; {note}" if note else "")})
    return {"lines": len(out_lines), "redacted": redacted, "rewritten": changed,
            "unparseable": unparseable, "counts": dict(counts), "verify": verify_chain(path)}
