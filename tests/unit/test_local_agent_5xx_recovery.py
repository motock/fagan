"""TDD tests for local_agent.recover_from_oversized_5xx — the escalating-trim
5xx recovery. The acceptance fixture tests/test_acceptance_5xx_escalation.py
grades the same helper; this file adds the 4xx-propagation case and is the
regression guard for the 2026-07-30 LAUNCHD-PLIST-PORTABILITY death (a single
trim-retry that also 500'd killed a ~95%-complete converging run).
"""
import importlib.util
import os
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("LOCAL_AGENT_MODEL", "dummy-for-tests")
_spec = importlib.util.spec_from_file_location(
    "local_agent_5xx_recovery_under_test",
    str(Path(__file__).parent.parent.parent / "scripts" / "local_agent.py"),
)
local_agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(local_agent)


def _5xx():
    req = httpx.Request("POST", "http://localhost:11434/api/chat")
    resp = httpx.Response(500, request=req)
    return httpx.HTTPStatusError("500 Internal Server Error", request=req, response=resp)


def _4xx():
    req = httpx.Request("POST", "http://localhost:11434/api/chat")
    resp = httpx.Response(400, request=req)
    return httpx.HTTPStatusError("400 Bad Request", request=req, response=resp)


def _m(role, content):
    return {"role": role, "content": content}



@pytest.fixture(autouse=True)
def _no_real_backoff(monkeypatch):
    """recover_from_oversized_5xx now sleeps between escalation rounds (2s/6s/
    12s) so a load-induced failure gets time to clear. Real sleeps cost this
    file ~20s per test; the pause itself is asserted explicitly where it
    matters, so stub it everywhere else."""
    monkeypatch.setattr(local_agent.time, "sleep", lambda _s: None)

@pytest.fixture
def always_shrinkable(monkeypatch):
    """Force _trim_resumed_transcript to report a shrinkage each call so the
    helper advances to chat_fn every escalation round — decouples this test
    from the trim helper's own behavior on small fixture messages."""

    def fake_trim(messages, max_chars):
        return list(messages)[:-1] if len(messages) > 1 else list(messages)

    monkeypatch.setattr(local_agent, "_trim_resumed_transcript", fake_trim)


def test_escalation_recovers_a_run_the_old_code_gave_up_on(always_shrinkable):
    """chat 5xx twice then succeed on the 3rd round -> helper returns the
    message (not None). The old trim-once/retry-once path gave up after the
    2nd 500 and killed the run."""
    calls = {"n": 0}

    def chat_fn(messages):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _5xx()
        return _m("assistant", "recovered")

    messages = [_m("system", "s"), _m("user", "u"), _m("assistant", "a"), _m("user", "x")]
    result = local_agent.recover_from_oversized_5xx(messages, chat_fn)
    assert result is not None and result["content"] == "recovered"
    assert calls["n"] >= 3


def test_persistent_5xx_gives_up_after_bounded_rounds(always_shrinkable):
    """A permanently-5xx backend must not loop forever: helper returns None
    after a bounded number of escalation rounds."""
    calls = {"n": 0}

    def chat_fn(messages):
        calls["n"] += 1
        raise _5xx()

    messages = [_m("system", "s"), _m("user", "u"), _m("assistant", "a"), _m("user", "x")]
    result = local_agent.recover_from_oversized_5xx(messages, chat_fn)
    assert result is None
    assert calls["n"] <= 5


def test_untrimmable_payload_retries_unchanged_after_backoff(monkeypatch):
    """A transcript the trim cannot shrink is positive evidence the failure
    was NOT a context overflow - which is exactly when backing off and
    retrying the same payload is the right remedy.

    Superseded the original assertion (give up immediately, chat never
    called) on 2026-08-07: that contract is what let a single transient
    Ollama 500 end a ~95%-complete converging run on 2026-07-30. The bound
    it was really protecting - never retry forever - is asserted below and
    in test_persistent_5xx_gives_up_after_bounded_rounds."""

    def no_shrink(messages, max_chars):
        return list(messages)  # unchanged

    monkeypatch.setattr(local_agent, "_trim_resumed_transcript", no_shrink)
    calls = {"n": 0}

    def chat_fn(messages):
        calls["n"] += 1
        if calls["n"] < 2:
            raise _5xx()
        return _m("assistant", "recovered")

    messages = [_m("system", "s"), _m("user", "u")]
    result = local_agent.recover_from_oversized_5xx(messages, chat_fn)
    assert result == _m("assistant", "recovered")
    assert 1 < calls["n"] <= 3, "must retry, but stay bounded"


def test_4xx_is_not_swallowed_it_propagates(always_shrinkable):
    """Only 5xx is escalation-worthy: a 4xx from chat_fn must propagate, not
    be swallowed into another trim-retry round."""
    calls = {"n": 0}

    def chat_fn(messages):
        calls["n"] += 1
        raise _4xx()

    messages = [_m("system", "s"), _m("user", "u")]
    with pytest.raises(httpx.HTTPStatusError):
        local_agent.recover_from_oversized_5xx(messages, chat_fn)
    assert calls["n"] == 1