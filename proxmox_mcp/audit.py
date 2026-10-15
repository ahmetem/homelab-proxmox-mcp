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
several MCP servers writing the same log; rewrite_redacted() takes the same
lock and rewrites in place, so it can't lose a line appended meanwhile.
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


def _unlock(fd: int, locked: bool) -> None:
    """Release a Windows byte lock; POSIX flock goes with os.close(fd)."""
    if locked and os.name == "nt":
        import msvcrt
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


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
            _unlock(fd, locked)
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


def _read_all(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while chunk := os.read(fd, 1 << 20):
        chunks.append(chunk)
    return b"".join(chunks)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def rewrite_redacted(path: Path, note: str = "", force: bool = False) -> dict:
    """Redact secrets already sitting in `path` and rebuild the hash chain.

    One-off maintenance for logs written before redaction existed. Every line
    keeps its position and fields; string fields go through the same redactor
    as append(). Chained lines get a recomputed `hash`, and each `prev` is
    mapped from the old hash to the new one, so an intact chain stays intact
    and a pre-existing `prev` break stays visible. Legacy lines stay unchained.
    When anything changed, a marker entry carrying `note` is appended. The
    redaction is not undoable: the original bytes are not kept anywhere.

    A line whose stored `hash` doesn't match its content (tampered, or written
    under another key) is never re-signed: re-hashing it would launder the
    edit. By default such a line makes the call refuse, writing nothing — the
    operator decides, e.g. archives the log as `<log>.<date>.kirik` and lets a
    fresh chain start. With `force=True` the rest is rewritten and those lines
    are only masked, keeping their stored `hash` and `prev`, so verify_chain
    still breaks on the same line; they are listed in `mismatched` and the
    marker note. If no chained line verifies at all, the key is wrong or
    missing (PROXMOX_AUDIT_HMAC_KEY not loaded): that refuses even with
    `force`, since it would strip the HMAC off a whole log.

    Concurrent appends are never lost. The slow redaction pass runs unlocked
    (a 6 MB log takes ~10 s, the Windows append lock gives up after ~10 s);
    then, under append()'s lock, the lines appended meanwhile are redacted too
    and the file is rewritten in place through the locked fd. Replacing the
    file instead would orphan an appender already waiting on the old inode's
    lock, and Windows refuses to replace a file another process has open. In
    place also keeps the log's owner and mode (a root-run rewrite on CT 215
    must not leave a root-owned log). The new content is first fsync'd to
    `<log>.redact-tmp`; if that file survives, the in-place write was cut
    short and it is the complete rewritten log; a later call refuses to run
    while it exists. Unlike a replace, the in-place write is not atomic for
    unlocked readers: a verify_chain overlapping it may report a transient
    break that is gone on retry.

    Returns {lines, redacted, rewritten, unparseable, mismatched: [line no],
    counts: {label: n}, verify}. Raises if the lock can't be had, the log
    shrank under it (rotated?), or on hash mismatches as above.
    """
    tmp = path.with_name(path.name + ".redact-tmp")
    if tmp.exists():  # O_TRUNC below would wipe the only complete copy
        raise RuntimeError(f"{tmp.name} present: a previous rewrite was cut short; "
                           "restore it over the log (or delete it) first")
    counts: Counter = Counter()
    key = _key()
    new_of: dict[str, str] = {}
    out_lines: list[str] = []
    mismatched: list[int] = []
    st = Counter()

    def redact_lines(data: bytes) -> None:
        for line in data.decode("utf-8", "replace").splitlines():
            try:
                obj = json.loads(line) if line.strip() else None
            except ValueError:
                obj = None
            if not isinstance(obj, dict):
                if line.strip():
                    st["unparseable"] += 1
                out_lines.append(line)
                continue
            red = {k: redact_counted(v, counts) if isinstance(v, str) and k not in ("hash", "prev") else v
                   for k, v in obj.items()}
            if any(red[k] != obj[k] for k in obj if k not in ("hash", "prev")):
                st["redacted"] += 1
            if "hash" in obj:
                old = obj["hash"]
                st["chained"] += 1
                if _digest({k: v for k, v in obj.items() if k != "hash"}, key) != old:
                    mismatched.append(len(out_lines) + 1)  # keeps stored hash/prev: never re-signed
                else:
                    red["prev"] = new_of.get(obj.get("prev") or "", obj.get("prev") or "")
                    red.pop("hash")
                    red["hash"] = _digest(red, key)
                    new_of[old] = red["hash"]
            if red != obj:
                st["changed"] += 1
                out_lines.append(json.dumps(red, ensure_ascii=False))
            else:
                out_lines.append(line)

    fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        raw = _read_all(fd)
        done = raw[:raw.rfind(b"\n") + 1]  # whole lines only; a line may be mid-append
        redact_lines(done)
        locked = _lock(fd)
        if not locked:
            raise RuntimeError("audit log lock not obtained; retry when idle")
        try:
            raw = _read_all(fd)
            if not raw.startswith(done):
                raise RuntimeError("log shrank or changed during rewrite (rotated?); nothing written")
            redact_lines(raw[len(done):])
            if mismatched and len(mismatched) == st["chained"]:
                raise RuntimeError(f"none of {st['chained']} chained lines verifies: "
                                   "PROXMOX_AUDIT_HMAC_KEY wrong or not loaded; nothing written")
            if mismatched and not force:
                raise RuntimeError(f"{len(mismatched)} line(s) fail their hash (tampered, or another key), "
                                   f"first at line(s) {mismatched[:5]}; nothing written. Archive the log "
                                   "(<log>.<date>.kirik) or pass force=True to mask the rest and keep "
                                   "those lines visibly broken")
            if st["changed"]:
                data = ("\n".join(out_lines) + "\n").encode("utf-8")
                tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
                try:
                    _write_all(tfd, data)
                    os.fsync(tfd)
                finally:
                    os.close(tfd)
                os.lseek(fd, 0, os.SEEK_SET)
                _write_all(fd, data)
                os.ftruncate(fd, len(data))
                os.fsync(fd)
                tmp.unlink()
        finally:
            _unlock(fd, locked)
    finally:
        os.close(fd)  # also releases the POSIX flock; append() below locks anew
    changed, redacted = st["changed"], st["redacted"]
    if not changed:
        return {"lines": len(out_lines), "redacted": 0, "rewritten": 0,
                "unparseable": st["unparseable"], "mismatched": mismatched, "counts": {},
                "verify": verify_chain(path)}
    passes = sum(counts.values())
    kept = (f"; {len(mismatched)} hash-mismatch line(s) kept broken, not re-signed: {mismatched[:20]}"
            if mismatched else "")
    append(path, {"ts": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "rc": None, "cmd": "",
                  "note": f"redaction rewrite: {redacted} lines masked ({passes} passes), "
                          f"{changed} rewritten, chain rebuilt" + kept + (f"; {note}" if note else "")})
    return {"lines": len(out_lines), "redacted": redacted, "rewritten": changed,
            "unparseable": st["unparseable"], "mismatched": mismatched, "counts": dict(counts),
            "verify": verify_chain(path)}
