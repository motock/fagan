"""Acceptance oracle: the macOS CI leg runs on master pushes and nightly only.

Issue 0e337f93-9624-4648-b1dd-a557e8209db3: in the last 150 runs the macOS leg
caught nothing Ubuntu did not, while 10 runs were lost to GitHub macOS runner
queueing/cancellation and stalled merges. The leg must stay in the workflow so
a macOS-only break is still found within a day, but it must not gate pull
requests.

Parsed as text on purpose: PyYAML is not a dependency of this repo, and an
importorskip would turn this whole oracle into a silent skip.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "ci.yml"
_README = _ROOT / "README.md"

_CRON = '- cron: "17 4 * * *"'
_PULL_REQUEST_PHRASE = "github.event_name == 'pull_request'"


def _workflow_text():
    return re.sub("#.*", "", _WORKFLOW.read_text())


def _top_level_block(text, key):
    """Lines belonging to a top-level `key:` mapping, up to the next one."""
    lines = text.splitlines()
    block = []
    inside = False
    for line in lines:
        if line.rstrip() == f"{key}:":
            inside = True
            continue
        if inside:
            if line and not line.startswith(" "):
                break
            block.append(line)
    return "\n".join(block)


def _os_expression_line(text):
    matches = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("os:") and "fromJSON" in line
    ]
    assert len(matches) == 1, f"expected exactly one matrix os: expression: {matches}"
    return matches[0]


def test_on_block_has_daily_schedule_cron():
    on_block = _top_level_block(_workflow_text(), "on")
    assert "schedule:" in on_block, f"top-level on: block has no schedule: key: {on_block!r}"
    assert _CRON in on_block, f"on: block has no nightly cron entry: {on_block!r}"


def test_matrix_os_line_uses_event_name():
    line = _os_expression_line(_workflow_text())
    assert _PULL_REQUEST_PHRASE in line, (
        "the matrix os: expression must branch on github.event_name so pull "
        f"requests skip macOS: {line!r}"
    )


def test_rationale_does_not_claim_a_pre_pr_signal():
    """The comment above the matrix must describe the new cadence.

    macOS no longer runs on pull requests, so a rationale that calls this leg
    a pre-PR signal contradicts the matrix expression right below it.
    """
    # Comments are wrapped, so normalise away the `#` markers and line breaks
    # before matching phrases.
    raw = " ".join(re.sub("#", " ", _WORKFLOW.read_text()).split())
    assert "pre-PR signal" not in raw, (
        "the ci.yml matrix rationale still calls the macOS leg a pre-PR signal, "
        "but macOS no longer runs on pull requests"
    )
    assert "caught within a day" in raw, (
        "the ci.yml matrix rationale must state that a macOS-only break is "
        "still caught within a day"
    )


def test_pull_request_branch_is_ubuntu_only_and_other_branch_has_both():
    line = _os_expression_line(_workflow_text())
    pull_request_part, _, other_part = line.partition("||")
    assert "'[\"ubuntu-latest\"]'" in pull_request_part, (
        f"pull requests must run ubuntu-latest only: {line!r}"
    )
    assert "macos-latest" not in pull_request_part, (
        f"macOS must not run on pull requests: {line!r}"
    )
    assert "'[\"ubuntu-latest\",\"macos-latest\"]'" in other_part, (
        f"push and schedule must still run both runners: {line!r}"
    )


def test_exclude_keeps_macos_off_311_312_313():
    text = _workflow_text()
    start = text.index("        exclude:")
    block = text[start:].split("\n    env:")[0]
    pairs = re.findall(
        r"- os:\s*(\S+)\s*\n\s*python-version:\s*(\"[^\"]+\")",
        block,
    )
    assert pairs == [
        ("macos-latest", '"3.11"'),
        ("macos-latest", '"3.12"'),
        ("macos-latest", '"3.13"'),
    ], f"macOS must stay on one Python version; exclude block is {pairs!r}"


def test_readme_mentions_master_and_nightly():
    readme = _README.read_text()
    assert "every push to master and nightly" in readme, (
        "README must document that macOS now runs on master pushes and nightly"
    )


def test_readme_no_longer_says_macos_on_every_push():
    readme = _README.read_text()
    assert "plus macOS on 3.14, on every" not in readme, (
        "README still claims macOS runs on every push"
    )
