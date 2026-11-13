"""Guard ADR 0012 decision 1: no store method returns a filesystem path.

``manifest_path`` is gone from the Store Protocol and from both implementations.
Every caller reaches plan manifests through the record view (``records()
.get_manifest`` and friends), so no production module may mention
``manifest_path``. This scans source text, not imports, so a caller added through
any route (a bare name, an attribute, a comment left behind) is caught.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED_ROOTS = ("pipeline", "app")
ALLOWED_FILES = frozenset()
NEEDLE = "manifest_path"


def find_manifest_path_mentions(roots: list[Path], base: Path) -> list[str]:
    """Return repo-relative POSIX paths of non-allowed files that mention the needle."""
    offenders = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(base).as_posix()
            if relative in ALLOWED_FILES:
                continue
            if NEEDLE in path.read_text(encoding="utf-8"):
                offenders.append(relative)
    return offenders


def test_manifest_path_is_mentioned_by_no_production_module():
    offenders = find_manifest_path_mentions(
        [REPO_ROOT / root for root in SCANNED_ROOTS], REPO_ROOT
    )

    assert offenders == [], (
        "manifest_path must not appear in any production module (ADR 0012 "
        "decision 1); move these callers onto records().get_manifest/"
        "put_manifest/list_plans: " + repr(offenders)
    )


def test_scanner_flags_a_new_store_manifest_path_caller(tmp_path):
    caller = tmp_path / "pipeline" / "synthetic_caller.py"
    caller.parent.mkdir()
    caller.write_text("def f(store, plan):\n    return store.manifest_path(plan)\n")

    offenders = find_manifest_path_mentions([tmp_path / "pipeline"], tmp_path)

    assert offenders == ["pipeline/synthetic_caller.py"]
