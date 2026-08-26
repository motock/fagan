"""Tests for the backend driver registry and OllamaDriver: the review-mode Bash+cwd read-only tool loop, REQUEST_CHANGES findings, review.log persistence, and transcript trimming.

Split out of test_backend.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._backend_helpers.
"""
import pytest

from app import backend as b
from app import backend_ollama as bo
from tests.unit._backend_helpers import (  # noqa: F401
    _LIVE_CHARS_PER_TOKEN,
    _clear_model_weights_cache,
    _envelope,
    _fake_review_chat,
    _review_trim_spy,
)


# ---------- complete() review mode (Bash + cwd -> read-only tool loop) ----------
def test_complete_runs_readonly_review_loop_when_bash_and_cwd(tmp_path, monkeypatch):
    """A review call (allowed_tools includes Bash + a worktree cwd) must run a
    tool loop — run tests / read files — then return text with a VERDICT, not
    a single-shot hallucinated verdict."""
    driver = b.OllamaDriver()
    responses = [
        _envelope({"tool_calls": [{"function": {"name": "bash", "arguments": {"command": "echo ran-tests"}}}]}),
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "APPROVE", "summary": "clean"}}}]}),
    ]
    tools_each_call = []
    monkeypatch.setattr(
        driver, "_chat",
        lambda messages, model, tools=None: tools_each_call.append(tools) or responses.pop(0),
    )

    out = driver.complete("review the branch", system="reviewer body",
                          model="sonnet", allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: APPROVE" in out
    assert tools_each_call[0] is not None          # the loop offered tools
    assert responses == []                          # consumed tool turn + submit_review


def test_complete_review_loop_recovers_unnamed_tool_call(tmp_path, monkeypatch):
    """devstral sometimes emits a bare args object with no tool name; the loop
    must infer the tool from its keys (here: verdict -> submit_review)."""
    driver = b.OllamaDriver()
    responses = [_envelope({"content": 'Looks good.\n{"verdict": "APPROVE", "summary": "ok"}'})]
    monkeypatch.setattr(driver, "_chat", lambda messages, model, tools=None: responses.pop(0))

    out = driver.complete("review", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: APPROVE" in out


def test_complete_review_loop_nudges_to_submit_near_step_cap(tmp_path, monkeypatch):
    """The local model investigates thoroughly but rarely converges to the
    submit_review terminator on its own (it WILL call it when pushed — the same
    model calls `done` in dispatch mode). When the step budget is nearly spent
    without a verdict, the loop must inject a convergence nudge pressing for
    submit_review, instead of silently exhausting into UNKNOWN (which parks the
    story and loops review/rework). Here the model keeps calling bash and never
    submits; the nudge must appear in the messages sent to the model."""
    driver = b.OllamaDriver()
    driver.review_max_steps = 4
    seen_user_msgs = []

    def chat(messages, model, tools=None):
        seen_user_msgs.extend(
            m.get("content") for m in messages if m.get("role") == "user"
        )
        return _envelope({"tool_calls": [{"function": {"name": "bash",
                                              "arguments": {"command": "ls"}}}]})

    monkeypatch.setattr(driver, "_chat", chat)
    driver.complete("review the branch", system="r", model="sonnet",
                    allowed_tools="Bash,Read", cwd=str(tmp_path))

    nudges = [c for c in seen_user_msgs if c and "submit_review" in c]
    assert nudges, "expected a convergence nudge mentioning submit_review near the cap"


def test_complete_review_loop_repeats_nudge_every_step_near_cap(tmp_path, monkeypatch):
    """A model that ignores the first convergence nudge and keeps calling
    bash must be re-nudged on EVERY subsequent step within the final window,
    not just once — a single nudge deep in a long agentic transcript is easy
    for a weak local model to lose track of. This is what let gpt-oss exhaust
    the full 20-step review cap in 3 of 5 observed review cycles on
    2026-07-04 (ratelimiter_inspect benchmark) despite the nudge firing once
    at 5-steps-remaining: the model just kept calling view_file/bash past it,
    with nothing reinforcing the instruction for the remaining steps."""
    driver = b.OllamaDriver()
    driver.review_max_steps = 6
    last_msg_had_nudge = []

    def chat(messages, model, tools=None):
        last_msg_had_nudge.append(
            bool(messages) and messages[-1].get("role") == "user"
            and "submit_review" in (messages[-1].get("content") or "")
        )
        return _envelope({"tool_calls": [{"function": {"name": "bash",
                                              "arguments": {"command": "ls"}}}]})

    monkeypatch.setattr(driver, "_chat", chat)
    driver.complete("review the branch", system="r", model="sonnet",
                    allowed_tools="Bash,Read", cwd=str(tmp_path))

    # review_max_steps=6 -> the "few steps left" window covers 5 calls
    # (5 down to 1 remaining). A model that never converges should be
    # re-nudged before most of those, not just the first one.
    nudge_count = sum(last_msg_had_nudge)
    assert nudge_count >= 3, (
        f"expected the nudge to repeat across multiple steps near the cap, "
        f"got {nudge_count} nudged calls: {last_msg_had_nudge}"
    )


def test_complete_review_loop_salvages_prose_verdict_on_cap(tmp_path, monkeypatch):
    """If the model writes its verdict as prose — a terminal 'VERDICT:' line as
    its conclusion — instead of calling submit_review, the loop must salvage it
    from the final assistant message rather than returning empty (empty ->
    UNKNOWN -> changes_requested with no feedback -> blind rework loop -> park).
    The verdict must be the LAST non-empty line (the model's actual conclusion);
    _parse_verdict stays strict otherwise."""
    driver = b.OllamaDriver()
    driver.review_max_steps = 2
    responses = [
        _envelope({"content": "Tests pass and the diff is clean. LGTM.\n\nVERDICT: APPROVE"}),
        _envelope({"content": "Tests pass and the diff is clean. LGTM.\n\nVERDICT: APPROVE"}),
    ]
    monkeypatch.setattr(driver, "_chat",
                        lambda messages, model, tools=None: responses.pop(0))

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: APPROVE" in out


def test_review_loop_reads_step_cap_live_from_env(tmp_path, monkeypatch):
    """PIPELINE_LOCAL_REVIEW_MAX_STEPS must take effect on a driver instance
    that already exists, mirroring how PIPELINE_LOCAL_MAX_STEPS is re-read
    live for dispatch (see the dispatch() num_ctx/max_steps re-read comment)
    rather than only being captured once at __init__ time. Without this, a
    plist/env edit to the review cap silently has no effect on a long-lived
    MCP server process (2026-07-07 web-client-epic retro §7 - this was the
    root cause of the observed config-propagation asymmetry)."""
    driver = b.OllamaDriver()
    assert driver.review_max_steps == 20  # constructor default, env unset
    monkeypatch.setenv("PIPELINE_LOCAL_REVIEW_MAX_STEPS", "2")
    call_count = 0

    def chat(messages, model, tools=None):
        nonlocal call_count
        call_count += 1
        return _envelope({"tool_calls": [{"function": {"name": "bash",
                                                        "arguments": {"command": "echo x"}}}]})
    monkeypatch.setattr(driver, "_chat", chat)

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    # A never-ending supply of tool-call responses is available (25+ would be
    # trivial for the mock to serve), so a call_count of 2 can only mean the
    # loop honored the live env override rather than the constructor's
    # captured default of 20.
    assert call_count == 2
    assert out == ""        # step cap exhausted with no verdict to salvage


def test_complete_review_loop_does_not_salvage_inline_verdict_mention(tmp_path, monkeypatch):
    """Fail-closed: prose that merely MENTIONS 'VERDICT: APPROVE' inline (the
    model echoing the convergence nudge, or describing what an approve would
    require) must NOT be salvaged to an APPROVE — only a terminal VERDICT: line
    counts. Otherwise the review loop could auto-merge unreviewed code on a
    non-verdict, violating Secure-by-Design (fail-open)."""
    driver = b.OllamaDriver()
    driver.review_max_steps = 2
    responses = [
        _envelope({"content": "I'll end my reply with a VERDICT: APPROVE line as the nudge "
                   "asked, but the tests actually fail and need fixing first."}),
        _envelope({"content": "I'll end my reply with a VERDICT: APPROVE line as the nudge "
                   "asked, but the tests actually fail and need fixing first."}),
    ]
    monkeypatch.setattr(driver, "_chat",
                        lambda messages, model, tools=None: responses.pop(0))

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: APPROVE" not in out, "inline VERDICT mention must not salvage to APPROVE"


def test_complete_review_loop_still_raises_runtime_error_on_http_error(tmp_path, monkeypatch):
    """The existing httpx.HTTPError handling in the review loop must be
    unchanged by broadening the catch to other exceptions - it must still
    raise RuntimeError with the review-specific message."""
    driver = b.OllamaDriver()

    def _boom(messages, model, tools=None):
        raise b.httpx.ConnectError("connection refused")

    monkeypatch.setattr(driver, "_chat", _boom)

    with pytest.raises(RuntimeError, match="errored during review"):
        driver.complete("review the branch", system="r", model="sonnet",
                        allowed_tools="Bash,Read", cwd=str(tmp_path))


def test_complete_review_loop_survives_non_http_error_from_chat(tmp_path, monkeypatch):
    """A non-HTTPError exception from _chat (e.g. a KeyError from a malformed
    Ollama response) must not crash the caller - the review loop must fail
    safe into the same 'no verdict' contract as exhausting the step cap
    without a submit_review call, rather than propagating and taking down
    the process."""
    driver = b.OllamaDriver()
    calls = {"n": 0}

    def chat(messages, model, tools=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _envelope({"tool_calls": [{"function": {"name": "bash",
                                                  "arguments": {"command": "echo hi"}}}]})
        raise KeyError("message")

    monkeypatch.setattr(driver, "_chat", chat)

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: APPROVE" not in out
    assert "VERDICT: REQUEST_CHANGES" not in out


def test_complete_review_loop_survives_malformed_tool_call_shape(tmp_path, monkeypatch):
    """A tool call missing the expected function/name keys (the model
    emitting a malformed tool call) must not crash the loop with a bare
    KeyError, and must not stop the review from converging on a later,
    well-formed submit_review call."""
    driver = b.OllamaDriver()
    responses = [
        _envelope({"tool_calls": [{"bogus": "shape"}]}),
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "APPROVE", "summary": "ok"}}}]}),
    ]
    monkeypatch.setattr(driver, "_chat", lambda messages, model, tools=None: responses.pop(0))

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: APPROVE" in out


# ---------- REQUEST_CHANGES must carry findings (REVIEW-FINDINGS) ----------
def test_complete_review_loop_rejects_findings_less_request_changes_once(tmp_path, monkeypatch):
    """A REQUEST_CHANGES with no summary/pr_body gives the redispatched agent
    nothing to act on. The loop must not accept it the first time - it should
    push back once with a corrective tool-role message, then accept the
    model's follow-up once it actually states the problems."""
    driver = b.OllamaDriver()
    responses = [
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "REQUEST_CHANGES"}}}]}),
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "REQUEST_CHANGES",
                                                    "summary": "auth.py:42 missing null check"}}}]}),
    ]
    seen_tool_msgs = []

    def chat(messages, model, tools=None):
        seen_tool_msgs.extend(m.get("content") for m in messages if m.get("role") == "tool")
        return responses.pop(0)

    monkeypatch.setattr(driver, "_chat", chat)

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: REQUEST_CHANGES" in out
    assert "auth.py:42 missing null check" in out
    corrective = [m for m in seen_tool_msgs if m and "file" in m.lower()]
    assert len(corrective) == 1, "expected exactly one corrective nudge for the findings-less verdict"


def test_complete_review_loop_fails_closed_after_two_findings_less_request_changes(tmp_path, monkeypatch):
    """If the model still submits no findings after the corrective nudge, the
    loop must fail closed: return REQUEST_CHANGES with whatever text exists,
    never silently upgrade to APPROVE, and never raise."""
    driver = b.OllamaDriver()
    responses = [
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "REQUEST_CHANGES"}}}]}),
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "REQUEST_CHANGES"}}}]}),
    ]
    monkeypatch.setattr(driver, "_chat", lambda messages, model, tools=None: responses.pop(0))

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: REQUEST_CHANGES" in out
    assert "APPROVE" not in out
    assert responses == []


def test_complete_review_loop_accepts_request_changes_with_summary_immediately(tmp_path, monkeypatch):
    """REQUEST_CHANGES with a non-empty summary on the first call must pass
    through untouched - no corrective nudge, no extra chat round-trip."""
    driver = b.OllamaDriver()
    calls = {"n": 0}

    def chat(messages, model, tools=None):
        calls["n"] += 1
        return _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                             "arguments": {"verdict": "REQUEST_CHANGES",
                                                           "summary": "tests fail in test_foo.py"}}}]})

    monkeypatch.setattr(driver, "_chat", chat)

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: REQUEST_CHANGES" in out
    assert "tests fail in test_foo.py" in out
    assert calls["n"] == 1


def test_complete_review_loop_accepts_approve_without_summary(tmp_path, monkeypatch):
    """APPROVE needs no findings - an empty summary must not trigger the
    corrective nudge that findings-less REQUEST_CHANGES gets."""
    driver = b.OllamaDriver()
    calls = {"n": 0}

    def chat(messages, model, tools=None):
        calls["n"] += 1
        return _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                             "arguments": {"verdict": "APPROVE"}}}]})

    monkeypatch.setattr(driver, "_chat", chat)

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: APPROVE" in out
    assert calls["n"] == 1


def test_complete_review_loop_findings_nudge_still_returns_verdict_near_step_cap(tmp_path, monkeypatch):
    """The findings nudge must not consume the step budget in a way that
    turns a conclusive review into UNKNOWN: even when it fires on the last
    available steps, a verdict must still come back once the model complies."""
    driver = b.OllamaDriver()
    driver.review_max_steps = 2
    responses = [
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "REQUEST_CHANGES"}}}]}),
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "REQUEST_CHANGES",
                                                    "summary": "foo.py:10 off by one"}}}]}),
    ]
    monkeypatch.setattr(driver, "_chat", lambda messages, model, tools=None: responses.pop(0))

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: REQUEST_CHANGES" in out
    assert "foo.py:10 off by one" in out


# ---------- review.log transcript persistence (REVIEW-LOG) ----------
def test_review_loop_writes_transcript_with_header_tool_and_verdict(tmp_path, monkeypatch):
    """The review loop must persist a plain-text transcript to review.log in
    the worktree cwd - a cycle header (timestamp + resolved model), each tool
    call, and the terminal verdict - so an inconclusive review is debuggable
    (dispatch's agent.log has no review counterpart today)."""
    driver = b.OllamaDriver()
    responses = [
        _envelope({"tool_calls": [{"function": {"name": "bash", "arguments": {"command": "echo ran-tests"}}}]}),
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "APPROVE", "summary": "clean"}}}]}),
    ]
    monkeypatch.setattr(driver, "_chat", lambda messages, model, tools=None: responses.pop(0))

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    log_path = tmp_path / "review.log"
    assert log_path.exists()
    content = log_path.read_text()
    assert "review cycle" in content
    assert "model=" in content
    assert "bash" in content
    assert "VERDICT: APPROVE" in content
    assert "VERDICT: APPROVE" in out


def test_review_loop_appends_across_multiple_review_cycles(tmp_path, monkeypatch):
    """Multiple review cycles in the same worktree cwd (a rework loop) must
    accumulate in one review.log, matching agent.log's append idiom - not
    truncate the previous cycle's transcript."""
    driver = b.OllamaDriver()

    def make_responses():
        return [
            _envelope({"tool_calls": [{"function": {"name": "bash", "arguments": {"command": "echo hi"}}}]}),
            _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                          "arguments": {"verdict": "APPROVE", "summary": "clean"}}}]}),
        ]

    for _ in range(2):
        responses = make_responses()
        monkeypatch.setattr(driver, "_chat", lambda messages, model, tools=None: responses.pop(0))  # noqa: B023 (existing test; lambda is consumed synchronously within the same loop iteration before rebinding, so this is not a real closure bug - not modified per workflow rule against touching tests without approval)
        driver.complete("review the branch", system="r", model="sonnet",
                        allowed_tools="Bash,Read", cwd=str(tmp_path))

    content = (tmp_path / "review.log").read_text()
    assert content.count("review cycle") == 2


def test_review_loop_survives_review_log_path_being_a_directory(tmp_path, monkeypatch):
    """Logging must never break the review: if review.log cannot be written
    (here, forced by pre-creating it as a directory so open() raises
    IsADirectoryError, an OSError subclass), the loop must still return its
    verdict and must not raise."""
    (tmp_path / "review.log").mkdir()
    driver = b.OllamaDriver()
    responses = [
        _envelope({"tool_calls": [{"function": {"name": "bash", "arguments": {"command": "echo hi"}}}]}),
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "APPROVE", "summary": "clean"}}}]}),
    ]
    monkeypatch.setattr(driver, "_chat", lambda messages, model, tools=None: responses.pop(0))

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: APPROVE" in out


def test_review_loop_truncates_tool_result_in_log_but_not_in_conversation(tmp_path, monkeypatch):
    """Tool results must be truncated to ~2000 chars in review.log only - the
    full text must still reach the model conversation unmodified."""
    driver = b.OllamaDriver()
    long_result = "X" * 3000
    monkeypatch.setattr(bo, "_run_readonly_tool", lambda fn, args, cwd: long_result)

    seen_messages = []
    responses = [
        _envelope({"tool_calls": [{"function": {"name": "bash", "arguments": {"command": "echo hi"}}}]}),
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "APPROVE", "summary": "clean"}}}]}),
    ]

    def chat(messages, model, tools=None):
        seen_messages.append(list(messages))
        return responses.pop(0)

    monkeypatch.setattr(driver, "_chat", chat)

    driver.complete("review the branch", system="r", model="sonnet",
                    allowed_tools="Bash,Read", cwd=str(tmp_path))

    tool_messages = [m["content"] for call in seen_messages for m in call
                     if m.get("role") == "tool"]
    assert long_result in tool_messages, "full tool result must reach the model conversation"

    content = (tmp_path / "review.log").read_text()
    assert long_result not in content, "full tool result must not appear unabridged in the log"
    assert "X" * 2000 in content, "truncated tool result must still appear in the log"


# ---------- review-loop transcript trimming (2026-07-29) ----------
# Unlike the dispatch agent loop, _review_loop appended every turn with zero
# trimming. Measured live from review_token_costs.jsonl: gpt-oss:20b review
# calls overflow a 16K context in 8.9% of turns (69/774), glm in 7.7%
# (280/3628) - the likely root cause of review_verdict=UNKNOWN-with-empty-
# feedback burning a rework cycle blind. Once a turn's real, measured
# prompt_eval_count is close to num_ctx, trim before the next turn instead
# of letting the transcript grow unbounded.

# These use a physically consistent fake: Ollama's prompt_eval_count covers
# the messages AND the tools schema, so the fake derives it from both at a
# fixed 2.35 chars/token - the live-measured ratio. A fake that counted only
# message chars would make the loop's own calibration look wrong when it
# isn't (and vice versa).






def test_review_loop_proactively_trims_when_measured_tokens_near_num_ctx(tmp_path, monkeypatch):
    """The trigger: an over-threshold measured prompt_eval_count must make
    the loop attempt a trim before the next turn."""
    driver = b.OllamaDriver()
    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "1000")
    monkeypatch.setattr(bo, "REVIEW_PROACTIVE_TRIM_THRESHOLD", 0.85)
    budgets = _review_trim_spy(monkeypatch)
    monkeypatch.setattr(driver, "_chat", _fake_review_chat())

    driver.complete("review", system="r", model="sonnet",
                    allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert budgets, "expected the accumulating transcript to trip the trim"


def test_review_loop_trim_preserves_head_and_drops_oldest(tmp_path, monkeypatch):
    """When the trim does drop content, the system+task head must survive and
    the dropped span must be replaced by the explanatory note."""
    messages = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "U"},
        {"role": "assistant", "content": "", "tool_calls": [{"a": "1"}]},
        {"role": "tool", "content": "x" * 500},
        {"role": "assistant", "content": "", "tool_calls": [{"a": "2"}]},
        {"role": "tool", "content": "y" * 500},
    ]
    out = b._trim_review_transcript(messages, 700)

    assert out[0] is messages[0] and out[1] is messages[1], "head must survive"
    assert out[2]["role"] == "user" and "dropped" in out[2]["content"]
    # The newest block is what's kept; the oldest is what went.
    assert messages[5] in out
    assert messages[3] not in out


def test_review_loop_trim_budget_is_calibrated_not_fixed_ratio(tmp_path, monkeypatch):
    """The sizing: the budget must come from the turn's OWN measured
    prompt_eval_count, not the fixed 4.0 guess. With the guess at
    num_ctx=1000 the target is 1000*4*0.75 = 3000 chars, which at the real
    2.35 ratio is ~1276 tokens - still ABOVE the 1000-token ceiling, so the
    'successful' trim would leave the prompt overflowing and fail to prevent
    the very 500 it exists to prevent."""
    driver = b.OllamaDriver()
    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "1000")
    monkeypatch.setattr(bo, "REVIEW_PROACTIVE_TRIM_THRESHOLD", 0.85)
    budgets = _review_trim_spy(monkeypatch)
    monkeypatch.setattr(driver, "_chat", _fake_review_chat())

    driver.complete("review", system="r", model="sonnet",
                    allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert budgets, "expected the proactive trim to fire at least once"
    fixed_ratio_budget = int(1000 * 4 * 0.75)
    for got in budgets:
        assert got != fixed_ratio_budget, (
            f"budget {got} is exactly the uncalibrated 4.0 guess - it must be "
            f"derived from the measured ratio"
        )
        assert got / _LIVE_CHARS_PER_TOKEN <= 1000, (
            f"budget {got} chars is ~{got / _LIVE_CHARS_PER_TOKEN:.0f} real "
            f"tokens, above the num_ctx=1000 ceiling - the trim would not "
            f"prevent the overflow"
        )


def test_review_loop_does_not_trim_when_below_threshold(tmp_path, monkeypatch):
    """No measurement, or one comfortably under threshold, must leave the
    transcript untouched - trimming is a targeted response to a real overflow
    risk, not a constant tax on every review."""
    driver = b.OllamaDriver()
    # A context window far larger than anything this short review accumulates,
    # so the threshold is never legitimately reached.
    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "100000")
    monkeypatch.setattr(bo, "REVIEW_PROACTIVE_TRIM_THRESHOLD", 0.85)
    budgets = _review_trim_spy(monkeypatch)
    monkeypatch.setattr(driver, "_chat", _fake_review_chat())

    driver.complete("review", system="r", model="sonnet",
                    allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert budgets == [], f"must not trim below threshold, attempted {budgets}"


def test_complete_stays_single_shot_for_overlord_style_call(monkeypatch):
    """Overlord-style complete() (allowed_tools='Read', no cwd) must NOT enter
    the tool loop — one plain completion, no tools offered."""
    driver = b.OllamaDriver()
    seen_tools = []
    monkeypatch.setattr(
        driver, "_chat",
        lambda messages, model, tools=None: seen_tools.append(tools) or _envelope({"content": "RULING: ROUTINE"}),
    )

    out = driver.complete("adjudicate", system="overlord", model="opus", allowed_tools="Read")

    assert out == "RULING: ROUTINE"
    assert seen_tools == [None]                      # single call, no tools


