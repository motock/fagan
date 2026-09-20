"""The reviewer prompt must carry a file-inventory criterion (item 5): the
reviewer signs off on the diff's file inventory, and any file the diff ADDS
that exists only because the work was carried out (a one-shot helper script,
a scratch/dump file, a copy of an existing module, an edit artifact) is a
Blocking finding that must be named on a `- Blocking: <path>: <desc>` line.

Regression context: a local dispatch committed a prescribed one-shot helper
script (`fix_escalation_teardown.py`, 21 lines of dead code at the repo root,
imported by nothing) and the review gate still returned APPROVE, because
nothing in the graded path asked "did this PR add a file nobody asked for".

The prompt is a shared artifact that later stories extend, so these tests
assert membership of the phrases this story owns plus ordering relative to
fixed anchors - never the prompt's total contents or length.
"""
from app import pipeline_mcp_server as p

# Item (5) as one contiguous sentence. The prompt is assembled from adjacent
# f-string literals, so this only holds if the fragments are joined with the
# right single spaces and in the right order.
FILE_INVENTORY_SENTENCE = (
    "(5) the diff's file inventory is part of what you are signing off on: "
    "list every file the diff ADDS and ask whether the change needed it."
)

# The delegation criterion (item 4) is the fixed anchor item (5) must follow.
DELEGATION_ANCHOR = "(4) if the change adds a module"


def _capture_review_prompt(monkeypatch):
    """Run _run_reviewer against a fake backend driver and return the prompt
    the reviewer persona was handed."""
    captured = {}

    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            captured["calls"] = captured.get("calls", 0) + 1
            return "VERDICT: APPROVE"

    monkeypatch.setattr(
        p.backend, "get_backend", lambda role, name=None: _FakeDriver()
    )
    result = p._run_reviewer("/tmp/some-worktree", "agent/some-branch")
    assert result == "VERDICT: APPROVE"
    assert captured["calls"] == 1
    prompt = captured["prompt"]
    assert isinstance(prompt, str) and prompt
    return prompt


def test_the_file_inventory_criterion_is_present_and_contiguous(monkeypatch):
    prompt = _capture_review_prompt(monkeypatch)
    assert FILE_INVENTORY_SENTENCE in prompt


def test_the_file_inventory_criterion_follows_the_delegation_criterion(
    monkeypatch,
):
    prompt = _capture_review_prompt(monkeypatch)
    assert "(5) the diff's file inventory" in prompt
    assert DELEGATION_ANCHOR in prompt
    assert prompt.index("(5) the diff's file inventory") > prompt.index(
        DELEGATION_ANCHOR
    )


def test_the_delegation_criterion_keeps_its_paragraph_break(monkeypatch):
    prompt = _capture_review_prompt(monkeypatch)
    # Item (5) must be its own fragment, not spliced onto the delegation
    # sentence - that would destroy the trailing blank line.
    assert "does not prove delegation is real.\n\n" in prompt
    assert "does not prove delegation is real.(5)" not in prompt


def test_the_file_inventory_criterion_names_the_blocking_line_format(
    monkeypatch,
):
    prompt = _capture_review_prompt(monkeypatch)
    assert "- Blocking: <relative/file/path>: <one-line description>" in prompt


def test_the_file_inventory_criterion_names_the_stray_artifact_class(
    monkeypatch,
):
    prompt = _capture_review_prompt(monkeypatch)
    assert "one-shot helper script" in prompt
    assert "scratch" in prompt
    assert "copy of an existing module" in prompt


def test_the_file_inventory_criterion_does_not_flag_expected_new_files(
    monkeypatch,
):
    prompt = _capture_review_prompt(monkeypatch)
    assert "expected, not a finding" in prompt


def test_the_prompt_still_ends_with_the_verdict_instruction(monkeypatch):
    prompt = _capture_review_prompt(monkeypatch)
    assert prompt.rstrip().endswith("also include a PR title and body.")
