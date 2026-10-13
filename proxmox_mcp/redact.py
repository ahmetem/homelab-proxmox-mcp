"""Secret redaction for the SSH audit logs.

The exec tools record the command and a preview of its output; both can carry
secrets (an inline `PGPASSWORD=…`, a `cat .env`, a DSN in `grep` output). Two
layers run before an entry is hashed and written:

1. Literal: every secret-looking value in this process's environment (the
   server's own `.env` is loaded into it by config.py, and on CT 207 the
   parent agent's env is inherited) is replaced by `<gizli:NAME>`. Selection
   is by value as well as by name — a name-only mask misses secrets stored
   under neutral names (`CT209_PG_HA`, `CT202_N8N_DB`).
2. Pattern: secrets that were never in this env (another CT's `.env` printed
   by a command) are caught by shape — `NAME=value` with a secret-ish name,
   credentials in a URL, `Bearer …`, PVE/PBS API tokens, `--password …`,
   PEM private keys, self-identifying tokens (`sk-ant-…`, `ghp_…`, Telegram)
   — and replaced by `<gizli>`. A base64 blob whose decoded text contains
   either kind is replaced whole by `<gizli:base64>`.

Over-redaction is the accepted failure mode; a value is never printed.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.parse
from collections import Counter
from typing import Optional

TAG = "<gizli>"

# Env names that hold identifiers/locations, not secrets (madde 227 list).
_EXCL_NAME = re.compile(
    r"(?:^|_)(HOST|PORT|USER|USERNAME|TOKEN_NAME|TOKEN_ID|NODE|DATASTORE|RACK_DN|"
    r"CHAT_ID|ALLOWED_USER_IDS|SSH_HOSTKEY|URL|KEY_PATH|KNOWN_HOSTS)$"
)
_SECRET_NAME = re.compile(r"PASS|PWD|SECRET|TOKEN|KEY|DSN|CREDENTIAL|(^|_)PAT(_|$)")
_DSN = re.compile(r"^[a-z][a-z0-9+.-]*://[^/@\s:]*:([^/@\s]+)@")
_URL = re.compile(r"^[a-z][a-z0-9+.-]*://")


def _env_secret(name: str, value: str) -> Optional[str]:
    """The part of an env value to mask literally, or None."""
    if _EXCL_NAME.search(name):
        return None
    m = _DSN.match(value)
    if m:
        return m.group(1)
    if len(value) < 8:
        return None
    if _SECRET_NAME.search(name.upper()):
        return None if _URL.match(value) else value
    if (
        _URL.match(value)
        or re.fullmatch(r"[\d.:]+", value)                       # number / IP
        or value.lower() in ("true", "false", "yes", "no")
        or re.fullmatch(r"[a-z][a-z_-]*", value)                 # plain word / slug
        or re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", value)        # hostname
        or re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", value)    # e-mail
        or re.match(r"([A-Za-z]:[\\/]|/|~/)", value)             # path
        or re.search(r"\s", value)                               # prose
    ):
        return None
    return value


def _literals() -> list[tuple[str, str]]:
    """(form, replacement) pairs from os.environ, longest form first."""
    seen: dict[str, str] = {}
    for name, value in os.environ.items():
        v = _env_secret(name, value.strip())
        if v and v not in seen:
            seen[v] = name
    pairs = []
    for v, name in seen.items():
        rep = f"<gizli:{name}>"
        for form in {v, urllib.parse.unquote(v), json.dumps(v)[1:-1],
                     urllib.parse.quote(v, safe="")}:
            if len(form) >= 8:
                pairs.append((form, rep))
    pairs.sort(key=lambda p: -len(p[0]))
    return pairs


_NAME = (
    r"[A-Za-z0-9_]*(?:PASS|PWD|SECRET|TOKEN|KEY|DSN|CREDENTIAL|_PAT\b|_PAT_|PAT_)[A-Za-z0-9_]*"
    r"|(?i:password|passwd|pwd|pw|secret|token|api_?key|access_?token|client_?secret)"
)
_KV = re.compile(
    r"\b(" + _NAME + r")([\"']?[ \t]*[=:][ \t]*)"
    r"(?:\"([^\"\n]*)\"|'([^'\n]*)'|([^\s\"'`;&|,}]+))"
)
_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
                re.DOTALL), "<gizli:private-key>"),
    (re.compile(r"\b([a-z][a-z0-9+.-]*://[^\s/@:'\"]*):(?!<gizli)[^\s/@'\"]+@"), r"\1:" + TAG + "@"),
    (re.compile(r"\b(PVEAPIToken|PBSAPIToken)=([^\s'\"!]+![^\s'\"=:]+)([=:])(?!<gizli)[^\s'\"]+",
                re.IGNORECASE), r"\1=\2\3" + TAG),
    (re.compile(r"\b(bearer|basic)\s+(?!<gizli)[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE), r"\1 " + TAG),
    (re.compile(r"(--(?:password|passwd|token|secret|api-?key|auth-token))(=|\s+)(?![<$])\S+",
                re.IGNORECASE), r"\1\2" + TAG),
    # Self-identifying tokens, whatever variable they sit in (T='sk-ant-oat01-…').
    (re.compile(r"\b(?:sk-ant-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}"
                r"|glpat-[A-Za-z0-9_-]{20,}|xox[abposr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}"
                r"|\d{8,10}:AA[A-Za-z0-9_-]{33})"), TAG),
]
# `echo '<base64>' | base64 -d > file` hides a file's content from every
# pattern above; a blob whose decoded text holds a secret is masked whole.
_B64 = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{24,}={0,2}")
B64_TAG = "<gizli:base64>"


_KV_NOT_SECRET = {"PWD", "OLDPWD", "NOPASSWD"}  # cwd vars, sudoers tag


def _kv_sub(m: re.Match) -> str:
    name, sep = m.group(1), m.group(2)
    if _EXCL_NAME.search(name.upper()) or name in _KV_NOT_SECRET:
        return m.group(0)
    if m.group(3) is not None:
        val, q = m.group(3), '"'
    elif m.group(4) is not None:
        val, q = m.group(4), "'"
    else:
        val, q = m.group(5), ""
    # Variable references, paths and plain counts (MAX_TOKENS=4096) are not secrets.
    if not val or val.startswith(("$", "<gizli", "/")) or re.fullmatch(r"\d{1,6}", val):
        return m.group(0)
    return f"{name}{sep}{q}{TAG}{q}"


def _decoded(blob: str) -> Optional[str]:
    blob = blob.rstrip("=")
    blob = blob[: len(blob) - len(blob) % 4]  # a cut-off preview still decodes
    try:
        text = base64.b64decode(blob, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return text if "".join(text.split()).isprintable() else None  # whitespace allowed


def redact_counted(text: str, counts: Optional[Counter] = None) -> str:
    """Redact `text`; if `counts` is given, add one per replacement by label."""
    if not text:
        return text
    text = _redact_plain(text, counts)

    def b64_sub(m: re.Match) -> str:
        dec = _decoded(m.group(0))
        if dec is None or _redact_plain(dec, None) == dec:
            return m.group(0)
        if counts is not None:
            counts[B64_TAG] += 1
        return B64_TAG
    return _B64.sub(b64_sub, text)


def _redact_plain(text: str, counts: Optional[Counter]) -> str:
    for form, rep in _literals():
        n = text.count(form)
        if n:
            text = text.replace(form, rep)
            if counts is not None:
                counts[rep] += n
    for pat, rep in _PATTERNS:
        text, n = pat.subn(rep, text)
        if n and counts is not None:
            counts[TAG if "private-key" not in rep else rep] += n
    before = text
    text = _KV.sub(_kv_sub, text)
    if counts is not None and text != before:
        counts[TAG] += sum(1 for m in _KV.finditer(before) if _kv_sub(m) != m.group(0))
    return text


def redact(text: str) -> str:
    return redact_counted(text)
