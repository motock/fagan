"""Tests for the review loop's post-cap final no-tools verdict call (RVF-1).

A local model that exhausts the tool budget without ever writing a terminal
VERDICT line still has its observations. After the existing terminal-line
salvage finds nothing, the loop makes ONE final call with no tools asking for
the conclusion. Fail closed: any error or unparseable answer falls through to
the existing no-verdict path ("" -> UNKNOWN -> park).

These drive `_review_loop` directly rather than `complete()`: the suite-wide
autouse `_hermetic_ollama_seams_default` fixture (tests/unit/conftest.py)
replaces `OllamaDriver.complete` with a "" stub for every test module not on
its `_REAL_OLLAMA_DRIVER_MODULES` allowlist, and the behavior under test -
the final verdict call - lives entirely inside `_review_loop`.
"""
from app import backend as b
from tests.unit._backend_helpers import _envelope


def _bash_tool_envelope() -> dict:
    return _envelope({"tool_calls": [{"function": {
        "name": "bash", "arguments": {"command": "echo x"}}}]})


def _run_review(driver, tmp_path) -> str:
    return driver._review_loop("review the branch", system="r", model="sonnet",
                               cwd=str(tmp_path))


def test_final_no_tools_call_supplies_verdict_after_step_cap(tmp_path, monkeypatch):
    """The model never writes a terminal VERDICT line during the loop; the
    final no-tools call does, and its verdict is returned."""
    driver = b.OllamaDriver()
    driver.review_max_steps = 2
    tools_seen = []

    def chat(messages, model, tools=None):
        tools_seen.append(tools)
        if tools is not None:
            return _bash_tool_envelope()
        return _envelope({"content": "VERDICT: REQUEST_CHANGES\n\nThe error path is untested."})

    monkeypatch.setattr(driver, "_chat", chat)

    out = _run_review(driver, tmp_path)

    assert out.startswith("VERDICT: REQUEST_CHANGES")
    assert "FINAL-VERDICT: REQUEST_CHANGES" in (tmp_path / "review.log").read_text()
    assert tools_seen[-1] is None          # the final call offered no tools
    assert tools_seen[:-1] == [driver._REVIEW_TOOLS] * 2


def test_final_call_unparseable_answer_falls_through_to_no_verdict(tmp_path, monkeypatch):
    """A final answer with no terminal VERDICT line must not be trusted."""
    driver = b.OllamaDriver()
    driver.review_max_steps = 2

    def chat(messages, model, tools=None):
        if tools is not None:
            return _bash_tool_envelope()
        return _envelope({"content": "I could not reach a conclusion."})

    monkeypatch.setattr(driver, "_chat", chat)

    out = _run_review(driver, tmp_path)

    assert out == ""
    assert "no verdict (step cap exhausted)" in (tmp_path / "review.log").read_text()


def test_final_call_error_falls_through_to_no_verdict(tmp_path, monkeypatch):
    """An error in the final call must never escape: fail closed to ""."""
    driver = b.OllamaDriver()
    driver.review_max_steps = 2

    def chat(messages, model, tools=None):
        if tools is not None:
            return _bash_tool_envelope()
        raise RuntimeError("backend went away")

    monkeypatch.setattr(driver, "_chat", chat)

    out = _run_review(driver, tmp_path)

    assert out == ""
    log = (tmp_path / "review.log").read_text()
    assert "final-verdict call failed (RuntimeError)" in log
    assert "no verdict (step cap exhausted)" in log


def test_final_call_inline_verdict_mention_is_not_salvaged(tmp_path, monkeypatch):
    """Fail closed: an inline 'VERDICT: APPROVE' that is not a standalone line
    must not be salvaged from the final answer."""
    driver = b.OllamaDriver()
    driver.review_max_steps = 2

    def chat(messages, model, tools=None):
        if tools is not None:
            return _bash_tool_envelope()
        return _envelope({"content": "I would write VERDICT: APPROVE here, but the tests fail."})

    monkeypatch.setattr(driver, "_chat", chat)

    out = _run_review(driver, tmp_path)

    assert out == ""
    assert "no verdict (step cap exhausted)" in (tmp_path / "review.log").read_text()


def test_terminal_verdict_in_transcript_skips_final_call(tmp_path, monkeypatch):
    """The existing salvage runs first: a terminal VERDICT line already in the
    transcript is returned without spending a final call."""
    driver = b.OllamaDriver()
    driver.review_max_steps = 2
    calls = []

    def chat(messages, model, tools=None):
        calls.append(tools)
        return _envelope({"content": "VERDICT: APPROVE"})

    monkeypatch.setattr(driver, "_chat", chat)

    out = _run_review(driver, tmp_path)

    assert out == "VERDICT: APPROVE"
    assert len(calls) == 2                 # loop steps only, no final call
