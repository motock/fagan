"""Tests for the mlx_lm.server wrapper entrypoint (scripts/mlx_server_wrapper.py).

mlx/mlx_lm are not installed in this project's venv (they only exist in
.venv-mlx) - apply_memory_limit()/instrument_handler() take the mx module and
handler class as parameters rather than importing them, so these tests can
exercise the real logic against fakes with no dependency on mlx-lm being
importable here.
"""
import importlib.util
import logging
import threading
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "mlx_server_wrapper", str(Path(__file__).parent / "scripts" / "mlx_server_wrapper.py")
)
wrapper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wrapper)


class _FakeMx:
    def __init__(self):
        self.limit_set_to = None
        self.peak_memory = 12345

    def set_memory_limit(self, limit_bytes):
        self.limit_set_to = limit_bytes

    def get_peak_memory(self):
        return self.peak_memory


def test_apply_memory_limit_converts_mb_to_bytes(monkeypatch):
    monkeypatch.setattr(wrapper, "MEMORY_LIMIT_MB", "22528")
    fake_mx = _FakeMx()

    applied = wrapper.apply_memory_limit(fake_mx)

    assert applied == 22528 * 1024 * 1024
    assert fake_mx.limit_set_to == 22528 * 1024 * 1024


def test_apply_memory_limit_honors_configured_value(monkeypatch):
    monkeypatch.setattr(wrapper, "MEMORY_LIMIT_MB", "1024")
    fake_mx = _FakeMx()

    applied = wrapper.apply_memory_limit(fake_mx)

    assert applied == 1024 * 1024 * 1024
    assert fake_mx.limit_set_to == 1024 * 1024 * 1024


class _FakeHandler:
    do_GET_calls = []
    do_POST_calls = []

    def __init__(self, path):
        self.path = path

    def do_GET(self):
        _FakeHandler.do_GET_calls.append(self.path)
        return "get-result"

    def do_POST(self):
        _FakeHandler.do_POST_calls.append(self.path)
        return "post-result"


def test_instrument_handler_preserves_return_value():
    _FakeHandler.do_GET_calls = []
    _FakeHandler.do_POST_calls = []
    fake_mx = _FakeMx()

    wrapper.instrument_handler(_FakeHandler, fake_mx)

    handler = _FakeHandler("/v1/models")
    assert handler.do_GET() == "get-result"
    assert handler.do_POST() == "post-result"


def test_instrument_handler_still_calls_original_method():
    _FakeHandler.do_GET_calls = []
    _FakeHandler.do_POST_calls = []
    fake_mx = _FakeMx()

    wrapper.instrument_handler(_FakeHandler, fake_mx)

    handler = _FakeHandler("/v1/chat/completions")
    handler.do_POST()

    assert _FakeHandler.do_POST_calls == ["/v1/chat/completions"]


def test_instrument_handler_logs_start_and_end_with_thread_name(caplog):
    _FakeHandler.do_GET_calls = []
    _FakeHandler.do_POST_calls = []
    fake_mx = _FakeMx()
    fake_mx.peak_memory = 999

    wrapper.instrument_handler(_FakeHandler, fake_mx)

    with caplog.at_level(logging.INFO, logger="mlx_server_wrapper"):
        handler = _FakeHandler("/v1/chat/completions")
        handler.do_POST()

    messages = [r.message for r in caplog.records]
    assert any("start" in m and "/v1/chat/completions" in m for m in messages)
    assert any("end" in m and "peak_bytes=999" in m for m in messages)
    assert any(threading.current_thread().name in m for m in messages)


def test_instrument_handler_propagates_exception_and_still_logs_end(caplog):
    class _FailingHandler:
        def do_GET(self):
            return "get-result"

        def do_POST(self):
            raise RuntimeError("boom")

    fake_mx = _FakeMx()
    wrapper.instrument_handler(_FailingHandler, fake_mx)

    handler = _FailingHandler()
    handler.path = "/v1/chat/completions"

    with caplog.at_level(logging.INFO, logger="mlx_server_wrapper"):
        try:
            handler.do_POST()
            raised = False
        except RuntimeError:
            raised = True

    assert raised is True
    messages = [r.message for r in caplog.records]
    assert any("end" in m for m in messages)
