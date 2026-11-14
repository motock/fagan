"""Regression guard for the merge-gate CI poll default (MERGE-CI-01).

``merge_ci_timeout_s`` is a tenant setting. Its default is 900 seconds, so a
six-job matrix (macOS worst case 5m39s) finishes before the merge gate gives
up. A stored value overrides the default for its (namespace, team, repo).
"""

from pathlib import Path

from pipeline import ci

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_MD = REPO_ROOT / "REFERENCE.md"


def test_default_ci_timeout_is_900(seed_merge_gate) -> None:
    timeout = ci.context_from_manifest({}).value("merge_ci_timeout_s")
    assert timeout == 900, (
        "merge_ci_timeout_s must default to 900 seconds so a six-job matrix "
        f"(macOS worst case 5m39s) finishes before the merge gate gives up; "
        f"got {timeout!r}"
    )


def test_stored_ci_timeout_overrides_the_900_default(seed_merge_gate) -> None:
    seed_merge_gate(merge_ci_timeout_s=120)
    timeout = ci.context_from_manifest({}).value("merge_ci_timeout_s")
    assert timeout == 120, (
        "A stored merge_ci_timeout_s must still override the 900 default; "
        f"got {timeout!r}"
    )


def test_reference_md_documents_default_900() -> None:
    rows = [
        line
        for line in REFERENCE_MD.read_text(encoding="utf-8").splitlines()
        if line.startswith("| `merge_ci_timeout_s`")
    ]
    assert rows and all("900" in row for row in rows), (
        "REFERENCE.md must document the merge_ci_timeout_s setting row with "
        f"default 900; found {rows!r}"
    )
