"""Regression pin for the bounded silent-read timeout on the streamed chat turn.

`_stream_one_turn` in both scripts/local_agent_chat.py and its oracle twin
passes `httpx.Timeout(connect=..., read=READ_SILENCE_SECONDS, ...)` to
`httpx.stream`. That read timeout is the ONLY thing that bounds a hung local
model: streaming means a legitimately slow-but-progressing generation never
trips it, while a genuine stall (no bytes for N seconds) does. Replacing
`read=origin["READ_SILENCE_SECONDS"]` with a literal (or dropping it back to
httpx's default) silently re-opens an unbounded silent-read hang - the Mode
32(d) shape - and the existing connect-timeout test would not notice, because
it asserts only `.connect`.
"""
import json

import pytest

from tests.unit._local_agent_oracle_test_helpers import (
    _FakeStreamCM as OracleCM,
)
from tests.unit._local_agent_oracle_test_helpers import (
    _FakeStreamResponse as OracleResponse,
)
from tests.unit._local_agent_oracle_test_helpers import (
    lao,
)
from tests.unit._local_agent_test_helpers import _FakeStreamCM, _FakeStreamResponse, la


@pytest.mark.parametrize(
    ("module", "cm", "response"),
    [
        pytest.param(la, _FakeStreamCM, _FakeStreamResponse, id="dispatch"),
        pytest.param(lao, OracleCM, OracleResponse, id="oracle"),
    ],
)
def test_stream_one_turn_bounds_a_silent_read_by_read_silence_seconds(
    monkeypatch, module, cm, response
):
    monkeypatch.setattr(module, "READ_SILENCE_SECONDS", 7.5)
    captured = {}

    def _fake_stream(method, url, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        lines = [json.dumps({"message": {"role": "assistant", "content": "hi"}, "done": True})]
        return cm(response(lines, status_code=200))

    monkeypatch.setattr(module.httpx, "stream", _fake_stream)
    module._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})

    assert captured["timeout"].read == 7.5, (
        "the streamed chat turn must bound a silent read by READ_SILENCE_SECONDS; "
        "a literal or default read timeout here re-opens an unbounded silent-read "
        "hang against a stalled local model"
    )
