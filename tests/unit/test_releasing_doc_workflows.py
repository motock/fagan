"""docs/RELEASING.md must account for every workflow in .github/workflows.

The release procedure enumerates the GitHub Actions workflows that exist and
lists them as pre-flight checks. A workflow that is added without a matching
line in `docs/RELEASING.md` is invisible to whoever cuts the next tag, so these
tests derive the workflow list from the directory instead of hard-coding it.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RELEASING = ROOT / "docs" / "RELEASING.md"
WORKFLOWS = ROOT / ".github" / "workflows"
STANDALONE_SMOKE = WORKFLOWS / "standalone-smoke.yml"


def _workflow_names() -> list[str]:
    return sorted(path.name for path in WORKFLOWS.glob("*.yml"))


def test_every_workflow_is_named_in_releasing_doc() -> None:
    doc = RELEASING.read_text(encoding="utf-8")
    names = _workflow_names()
    # Guard against the directory listing silently coming back empty.
    assert names, f"no *.yml workflows found in {WORKFLOWS}"
    missing = [name for name in names if name not in doc]
    assert not missing, (
        "docs/RELEASING.md does not mention these workflows: "
        f"{', '.join(missing)} (found in {WORKFLOWS})"
    )


def test_releasing_doc_does_not_claim_two_workflows() -> None:
    doc = RELEASING.read_text(encoding="utf-8")
    assert "Two workflows" not in doc, (
        "docs/RELEASING.md still counts the workflows as two; "
        f"there are {len(_workflow_names())} in {WORKFLOWS}"
    )


def test_releasing_doc_runs_standalone_smoke_on_demand() -> None:
    doc = RELEASING.read_text(encoding="utf-8")
    assert "gh workflow run standalone-smoke.yml" in doc, (
        "docs/RELEASING.md must tell the releaser to start the standalone smoke "
        "by hand, since it does not run on pushes to master"
    )


def test_standalone_smoke_header_no_longer_says_being_brought_to_green() -> None:
    text = STANDALONE_SMOKE.read_text(encoding="utf-8")
    # The old wording wraps across two comment lines, so look for the fragment
    # that survives the wrap rather than the whole sentence.
    assert "being brought" not in text, (
        "standalone-smoke.yml's header still says the run is 'being brought to "
        "green'; the run is green on master now"
    )


def test_standalone_smoke_keeps_its_explicit_gate() -> None:
    text = STANDALONE_SMOKE.read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines()]
    assert "if: vars.ENABLE_STANDALONE_SMOKE_CI == 'true'" in lines, (
        "standalone-smoke.yml must keep the explicit `== 'true'` gate: a bare "
        "`vars.ENABLE_STANDALONE_SMOKE_CI` is truthy for the string \"false\""
    )
