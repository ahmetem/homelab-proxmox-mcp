"""Char-budget and correctness tests for proxmox_mcp.format helpers.

Focus: the two output-capping helpers `truncate` and `compact_json` that guard
the model's context from oversized MCP responses. Runnable either with pytest
(`python -m pytest tests/`) or standalone (`python tests/test_format.py`), so it
works even though the repo has no test dependency pinned.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proxmox_mcp.format import compact_json, project_fields, truncate  # noqa: E402


def test_truncate_passthrough_under_limit():
    s = "x" * 100
    assert truncate(s, limit=200) == s


def test_truncate_caps_over_limit():
    s = "x" * 5000
    out = truncate(s, limit=1000)
    # Body is capped at the limit; a marker is appended noting the original size.
    assert out.startswith("x" * 1000)
    assert "truncated, total 5000 chars" in out
    # The whole thing stays close to the budget (limit + short marker), never 5000.
    assert len(out) < 1100


def test_truncate_default_limit_is_bounded():
    s = "y" * 100_000
    out = truncate(s)
    assert len(out) < 8100  # default limit 8000 + marker


def test_compact_json_has_no_indent_whitespace():
    obj = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    out = compact_json(obj)
    assert "\n" not in out
    assert ", " not in out  # compact separators, not the ", "/": " defaults
    assert ": " not in out


def test_compact_json_is_valid_json_under_limit():
    obj = {"vmid": 101, "name": "hass", "mem": 2048, "tags": ["a", "b"]}
    out = compact_json(obj)
    assert json.loads(out) == obj


def test_compact_json_is_smaller_than_indented():
    obj = [{"vmid": i, "name": f"guest-{i}", "status": "running"} for i in range(50)]
    compact = compact_json(obj, limit=10_000_000)
    indented = json.dumps(obj, indent=2)
    assert len(compact) < len(indented)


def test_compact_json_hard_caps_large_payload():
    # 20k list entries would be a huge dump; the cap must bound it.
    obj = [{"i": i, "blob": "z" * 50} for i in range(20_000)]
    out = compact_json(obj)
    assert len(out) < 8100
    assert "truncated" in out


def test_compact_json_truncated_list_stays_valid_json():
    obj = [{"i": i, "blob": "z" * 50} for i in range(20_000)]
    out = compact_json(obj)
    parsed = json.loads(out)  # must not raise
    assert isinstance(parsed, list)
    # Marker records exactly how many items were cut.
    marker = parsed[-1]
    assert set(marker) == {"_truncated_items"}
    assert len(parsed) - 1 + marker["_truncated_items"] == 20_000
    # Kept items are unmodified.
    assert parsed[0] == obj[0]


def test_compact_json_huge_single_dict_falls_back_to_hard_cap():
    obj = {"blob": "z" * 50_000}
    out = compact_json(obj)
    assert len(out) < 8100
    assert "truncated" in out


def test_project_fields_filters_dicts_and_lists():
    obj = [{"vmid": 101, "name": "a", "cpu": 0.1}, {"vmid": 102, "name": "b"}]
    assert project_fields(obj, ["vmid"]) == [{"vmid": 101}, {"vmid": 102}]
    assert project_fields({"a": 1, "b": 2}, ["b"]) == {"b": 2}
    # None / empty pass through; non-dicts untouched.
    assert project_fields(obj, None) == obj
    assert project_fields(obj, []) == obj
    assert project_fields("text", ["x"]) == "text"


def test_compact_json_applies_fields_projection():
    obj = [{"vmid": i, "name": f"g{i}", "blob": "z" * 100} for i in range(5)]
    out = compact_json(obj, fields=["vmid"])
    assert json.loads(out) == [{"vmid": i} for i in range(5)]


def test_compact_json_default_str_fallback():
    class Weird:
        def __str__(self):
            return "weird-value"

    out = compact_json({"k": Weird()})
    assert "weird-value" in out


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
