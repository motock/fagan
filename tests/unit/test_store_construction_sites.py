"""Every ``FileStore`` construction goes through ``pipeline.store.build_store``.

The plan store is selected in one place (``pipeline.store.build_store``) so
``PIPELINE_STORE`` governs the whole process. A stray ``FileStore()`` at a call
site bypasses that selection: it keeps writing JSON files in the plan directory
even when the operator selected another store, and it fails to raise on a
configuration the factory rejects.

This scans the source rather than importing the modules, so a construction
hidden inside a function body (as in ``pipeline.authorization._load_manifest``)
is caught too. It asserts membership and absence, never a count, so the
guard stays valid as call sites are added and removed.

The scan is a textual tripwire: it matches the literal ``FileStore()`` anywhere
in a file, so a comment or docstring that spells the expression out would be
reported as an offending site. That false positive is deliberate — it is
cheaper to reword a comment than to let a real call site slip through — so
prose should refer to "constructing the file store" instead of the expression.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FACTORY_MODULE = "pipeline/store.py"
CONSTRUCTION = "FileStore()"


def _construction_sites() -> set[str]:
    """Repo-relative paths of modules that construct a ``FileStore`` directly."""
    sites: set[str] = set()
    for package in ("pipeline", "app"):
        for path in (REPO_ROOT / package).rglob("*.py"):
            if CONSTRUCTION in path.read_text(encoding="utf-8", errors="replace"):
                sites.add(path.relative_to(REPO_ROOT).as_posix())
    return sites


def test_factory_module_still_constructs_the_file_store():
    """The factory itself is the one place allowed to construct it."""
    assert FACTORY_MODULE in _construction_sites()


def test_no_other_module_constructs_a_file_store_directly():
    """Call sites must go through the factory so ``PIPELINE_STORE`` governs them."""
    offenders = _construction_sites() - {FACTORY_MODULE}
    assert not offenders, (
        "these modules construct FileStore() directly instead of calling "
        f"pipeline.store.build_store(): {sorted(offenders)}"
    )
