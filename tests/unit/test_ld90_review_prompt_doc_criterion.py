"""The reviewer prompt's documentation criterion (item 3) must read as one
contiguous sentence, and the wrapper/adapter delegation check (item 4) must
follow it as its own item.

Regression context: item (4)'s text was pasted into the middle of item (3)'s
documentation sentence, which both truncated item (3) (the words "treat every
doc gap as a blocker. If this change alters behavior " were lost) and left the
rendered prompt reading "... does not prove delegation is real.that EXISTING
callers/users already depend on ...".

The prompt is a shared artifact that later stories extend, so these tests
assert membership of the phrases this story owns plus ordering relative to
fixed anchors - never the prompt's total contents or length.
"""
from app import pipeline_mcp_server as p

# Item (3) as one contiguous sentence. The prompt is assembled from adjacent
# f-string literals, so this only holds if the fragments are joined with the
# right single spaces and in the right order.
DOC_CRITERION_SENTENCE = (
    "(3) documentation - but calibrate this to our Blocking-vs-Suggestion "
    "policy, don't treat every doc gap as a blocker. If this change alters "
    "behavior that EXISTING callers/users already depend on (a public API "
    "contract, configuration, CLI flag, or user-facing functionality that "
    "predates this change) and no documentation update accompanies it, "
    "that's a genuine problem: REQUEST_CHANGES and name the specific doc "
    "(a README or other in-repo doc) that needs updating."
)

# Item (4) verbatim, with its hyphen in "round-trip" as a plain ASCII hyphen.
DELEGATION_CRITERION = (
    "(4) if the change adds a module that other production files import from "
    "(a wrapper/adapter/binding shim), it must trace what that module "
    "actually calls and flag any module that reimplements logic it should "
    "delegate to as Blocking. For example, replacing a crypto/WASM/native "
    "binding with a pure-language no-op or a base64 round-trip placeholder "
    "is not sufficient evidence; a green test suite alone does not prove "
    "delegation is real."
)


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


def test_documentation_criterion_is_one_contiguous_sentence(monkeypatch):
    prompt = _capture_review_prompt(monkeypatch)
    assert DOC_CRITERION_SENTENCE in prompt


def test_delegation_criterion_follows_the_documentation_criterion(monkeypatch):
    prompt = _capture_review_prompt(monkeypatch)
    assert "(4) if the change adds a module" in prompt
    assert prompt.index("(4) if the change adds a module") > prompt.index(
        "correct and tested"
    )
    assert prompt.index("(4)") > prompt.index("(3)")


def test_delegation_criterion_is_not_embedded_in_the_documentation_sentence(
    monkeypatch,
):
    prompt = _capture_review_prompt(monkeypatch)
    # The malformed splice: item (4) sat between "blocker;" and item (3)'s
    # continuation, so the two sentences ran together.
    assert "delegation is real.that" not in prompt
    assert "blocker; (4)" not in prompt
    # Item (3) no longer ends where item (4) used to be spliced in.
    assert "correct and tested.\n\n" not in prompt


def test_delegation_criterion_is_complete(monkeypatch):
    prompt = _capture_review_prompt(monkeypatch)
    assert DELEGATION_CRITERION in prompt
    assert "does not prove delegation is real." in prompt
    assert "does not prove delegation is real.\n\n" in prompt


def test_documentation_criterion_ends_before_the_delegation_criterion(
    monkeypatch,
):
    prompt = _capture_review_prompt(monkeypatch)
    # Item (3)'s closing clause is intact and hands off directly to item (4).
    assert (
        "REQUEST_CHANGES for that reason alone if the code itself is "
        "correct and tested; (4) if the change adds a module" in prompt
    )


def test_delegation_criterion_uses_an_ascii_hyphen_in_round_trip(monkeypatch):
    prompt = _capture_review_prompt(monkeypatch)
    assert "base64 round-trip placeholder" in prompt
    # U+2011 NON-BREAKING HYPHEN must not survive in the rendered prompt.
    assert "round\u2011trip" not in prompt


def test_surviving_prompt_content_is_preserved(monkeypatch):
    prompt = _capture_review_prompt(monkeypatch)
    # Items (1) and (2) are untouched by this story.
    assert (
        "(1) any function taking a mutable argument (list, dict, set) does "
        "not mutate it in place unless that is the documented contract" in prompt
    )
    assert (
        "(2) inputs are validated at system boundaries, including "
        "negative/out-of-range numeric arguments, not just the happy path"
        in prompt
    )
    # Item (3)'s Suggestion carve-out for brand-new additions survives.
    assert "For a brand-new addition with no " in prompt
    assert "a missing doc update is a " in prompt
    assert "Suggestion, not a blocker" in prompt
    # The verdict/PR-title instruction survives.
    assert (
        "End with your VERDICT line; if you APPROVE, "
        "also include a PR title and body." in prompt
    )
