"""The package version must match pyproject.toml -- one number, two files.

Rationale: `proxmox_mcp.__version__` is public API (it is in `__all__`), while
`pyproject.toml` is what pip installs and what CHANGELOG.md tracks. Between
2026-07-30 and 2026-09-03 they disagreed (1.3.0 vs 1.5.0) and nothing noticed,
so clients importing the package were told the wrong version for five weeks.
"""
from __future__ import annotations

import pathlib
import sys
import tomllib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

PYPROJECT = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"


def _pyproject_version() -> str:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def test_dunder_version_matches_pyproject() -> None:
    import proxmox_mcp

    assert proxmox_mcp.__version__ == _pyproject_version()


def test_changelog_documents_current_version() -> None:
    """The shipped version must have a CHANGELOG entry -- no silent releases."""
    changelog = (PYPROJECT.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"[{_pyproject_version()}]" in changelog


if __name__ == "__main__":
    test_dunder_version_matches_pyproject()
    test_changelog_documents_current_version()
    print("ok")
