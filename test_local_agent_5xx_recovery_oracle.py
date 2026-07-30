"""TDD tests for local_agent_oracle.recover_from_oversized_5xx — the escalating-
trim 5xx recovery ported from local_agent.py. The oracle runs the production
path for acceptance-bearing dispatches (oracle_mode = bool(acceptance) in
backend.py), so the 2026-07-30 LAUNCHD-PLIST-PORTABILITY death happened on THIS
script; the fix must land here too. The acceptance fixture
tests/test_acceptance_5xx_escalation_oracle.py grades the same helper; this file
adds the 4xx-propagation case and is the regression guard.
"""
import importlib.util
import os
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("LOCAL_AGENT_MODEL", "dummy-for-tests")
_spec = importlib.util.spec_from_file_location(
    "local_agent_oracle_5xx_recovery_under_test",
    str(Path(__file__).parent / "scripts" / "local_agent_oracle.py"),
)
local_agent_oracle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(local_agent_oracle)


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


@pytest.fixture
def always_shrinkable(monkeypatch):
    """Force _trim_resumed_transcript to report a shrinkage each call so the
    helper advances to chat_fn every escalation round — decouples this test
    from the trim helper's own behavior on small fixture messages."""

    def fake_trim(messages, max_chars):
        return list(messages)[:-1] if len(messages) > 1 else list(messages)

    monkeypatch.setattr(local_agent_oracle, "_trim_resumed_transcript", fake_trim)


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
    result = local_agent_oracle.recover_from_oversized_5xx(messages, chat_fn)
    assert result is not None and result["content"] == "recovered"
    assert calls["n"] >= 3, f"escalation did not retry beyond one attempt: {calls['n']}"


def test_persistent_5xx_gives_up_after_bounded_rounds(always_shrinkable):
    """A permanently-5xx backend must not loop forever: helper returns None
    after a bounded number of escalation rounds."""
    calls = {"n": 0}

    def chat_fn(messages):
        calls["n"] += 1
        raise _5xx()

    messages = [_m("system", "s"), _m("user", "u"), _m("assistant", "a"), _m("user", "x")]
    result = local_agent_oracle.recover_from_oversized_5xx(messages, chat_fn)
    assert result is None
    assert calls["n"] <= 5, f"escalation did not bound retries: {calls['n']} calls"


def test_untrimmable_payload_gives_up_without_calling_chat(monkeypatch):
    """If a trim cannot shrink the transcript at all, the helper must give up
    immediately rather than retry an identical (still-oversized) payload."""

    def no_shrink(messages, max_chars):
        return list(messages)  # unchanged

    monkeypatch.setattr(local_agent_oracle, "_trim_resumed_transcript", no_shrink)
    calls = {"n": 0}

    def chat_fn(messages):
        calls["n"] += 1
        return _m("assistant", "should-not-happen")

    messages = [_m("system", "s"), _m("user", "u")]
    result = local_agent_oracle.recover_from_oversized_5xx(messages, chat_fn)
    assert result is None
    assert calls["n"] == 0, "helper must not call chat_fn when trim cannot shrink"


def test_4xx_is_not_swallowed_it_propagates(always_shrinkable):
    """Only 5xx is escalation-worthy: a 4xx from chat_fn must propagate, not
    be swallowed into another trim-retry round."""
    calls = {"n": 0}

    def chat_fn(messages):
        calls["n"] += 1
        raise _4xx()

    messages = [_m("system", "s"), _m("user", "u")]
    with pytest.raises(httpx.HTTPStatusError):
        local_agent_oracle.recover_from_oversized_5xx(messages, chat_fn)
    assert calls["n"] == 1