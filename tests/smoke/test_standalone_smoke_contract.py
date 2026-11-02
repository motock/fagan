"""Contract tests for the gated standalone smoke (SA-18).

The standalone smoke proves the operator path end to end in CI: `deploy/install.sh
--non-interactive` brings up deploy/standalone/compose.yaml, then
deploy/standalone/smoke.sh drives a `repo_url` plan through the MCP transport.

These tests guard the two things that silently break such a job: a workflow that
is not actually gated (so it runs, or never runs, when it should do the other),
and an `uses:` pinned to a mutable tag instead of a commit SHA. PyYAML is not a
dependency of this repo, so the files are read as text.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "standalone-smoke.yml"
SCRIPT = REPO_ROOT / "deploy" / "standalone" / "smoke.sh"

GATE = re.compile(r"ENABLE_STANDALONE_SMOKE_CI\s*==\s*'true'")
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)", re.MULTILINE)
SHA = re.compile(r"^[0-9a-f]{40}$")


def _uses_refs(text: str) -> list[str]:
    """The git ref of every `uses:` line: the text after the last '@'."""
    refs = []
    for action in USES.findall(text):
        ref = action.rsplit("@", 1)[-1]
        refs.append(ref.split("#", 1)[0].strip())
    return refs


def _is_sha_ref(ref: str) -> bool:
    return bool(SHA.match(ref))


def test_workflow_exists():
    assert WORKFLOW.exists(), f"{WORKFLOW} missing"


def test_job_is_gated_on_the_repository_variable():
    assert WORKFLOW.exists(), f"{WORKFLOW} missing"
    text = WORKFLOW.read_text(encoding="utf-8")
    assert GATE.search(text), (
        "the standalone smoke job must be gated with "
        "`if: vars.ENABLE_STANDALONE_SMOKE_CI == 'true'`; a bare "
        "`ENABLE_STANDALONE_SMOKE_CI` is truthy for the string \"false\""
    )


def test_every_action_is_pinned_to_a_full_commit_sha():
    assert WORKFLOW.exists(), f"{WORKFLOW} missing"
    refs = _uses_refs(WORKFLOW.read_text(encoding="utf-8"))
    assert refs, "no `uses:` line found in the workflow"
    unpinned = [ref for ref in refs if not _is_sha_ref(ref)]
    assert not unpinned, f"`uses:` refs that are not 40-hex commit SHAs: {unpinned}"


def test_script_parses():
    assert SCRIPT.exists(), f"{SCRIPT} missing"
    proc = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"bash -n rejected {SCRIPT}:\n{proc.stderr}"


def test_script_registers_a_teardown_trap():
    assert SCRIPT.exists(), f"{SCRIPT} missing"
    text = SCRIPT.read_text(encoding="utf-8")
    assert "trap" in text, (
        "smoke.sh must register `trap cleanup EXIT` before the stack starts, so a "
        "failure mid-run still runs `compose down -v` and the next run is clean"
    )


def test_sha_validator_covers_the_negative_case():
    # The pinning check above is only meaningful if it actually rejects a tag.
    assert not _is_sha_ref("v4")
    assert _is_sha_ref("0123456789abcdef0123456789abcdef01234567")


def test_gate_regex_covers_the_negative_case():
    # A gate without the explicit comparison would run when the variable is "false".
    assert not GATE.search("    if: vars.ENABLE_STANDALONE_SMOKE_CI")
