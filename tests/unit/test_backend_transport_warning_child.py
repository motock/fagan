"""The transport-only warning is silent in a dispatched child process.

The dispatcher sets PIPELINE_CORRELATION_ID in every dispatched agent's
environment, and the Ollama driver sets the transport variables in the same
child. A child must not warn about values its own parent set; an operator's
long-lived process (no correlation id) must still warn.
"""

import importlib
import logging

import pytest

import app.backend
from pipeline.config_provenance import ignored_env_vars_present

_TRANSPORT_VARS = (
    "PIPELINE_TRANSPORT_NUM_CTX",
    "PIPELINE_TRANSPORT_TEMPERATURE",
    "PIPELINE_TRANSPORT_MAX_STEPS",
)
_CORRELATION = "PIPELINE_CORRELATION_ID"


def _reload_with_env(monkeypatch, set_vars=None, unset=()):
    for name in unset:
        monkeypatch.delenv(name, raising=False)
    for name, value in (set_vars or {}).items():
        monkeypatch.setenv(name, value)
    return importlib.reload(app.backend)


def _transport_warnings(caplog):
    return [
        r
        for r in caplog.records
        if r.name == "pipeline"
        and r.levelno == logging.WARNING
        and any(name in r.getMessage() for name in _TRANSPORT_VARS)
    ]


@pytest.fixture(autouse=True)
def _restore_clean_backend(monkeypatch):
    yield
    for name in (*_TRANSPORT_VARS, _CORRELATION):
        monkeypatch.delenv(name, raising=False)
    importlib.reload(app.backend)


class TestTransportWarningInDispatchedChild:
    def test_warns_for_transport_var_without_correlation_id(self, monkeypatch, caplog):
        with caplog.at_level(logging.WARNING, logger="pipeline"):
            _reload_with_env(
                monkeypatch,
                {"PIPELINE_TRANSPORT_NUM_CTX": "1"},
                unset=(_CORRELATION,),
            )
        warnings = _transport_warnings(caplog)
        assert len(warnings) == 1
        assert "PIPELINE_TRANSPORT_NUM_CTX" in warnings[0].getMessage()

    def test_no_warning_for_transport_var_with_correlation_id(self, monkeypatch, caplog):
        with caplog.at_level(logging.WARNING, logger="pipeline"):
            _reload_with_env(
                monkeypatch,
                {"PIPELINE_TRANSPORT_NUM_CTX": "1", _CORRELATION: "abc"},
            )
        assert _transport_warnings(caplog) == []

    def test_correlation_id_presence_decides_not_truthiness(self, monkeypatch, caplog):
        with caplog.at_level(logging.WARNING, logger="pipeline"):
            _reload_with_env(
                monkeypatch,
                {"PIPELINE_TRANSPORT_NUM_CTX": "1", _CORRELATION: ""},
            )
        assert _transport_warnings(caplog) == []

    def test_no_warning_for_all_transport_vars_with_correlation_id(self, monkeypatch, caplog):
        with caplog.at_level(logging.WARNING, logger="pipeline"):
            _reload_with_env(
                monkeypatch,
                {
                    "PIPELINE_TRANSPORT_NUM_CTX": "1",
                    "PIPELINE_TRANSPORT_TEMPERATURE": "0.2",
                    "PIPELINE_TRANSPORT_MAX_STEPS": "5",
                    _CORRELATION: "abc",
                },
            )
        assert _transport_warnings(caplog) == []

    def test_no_warning_when_no_transport_var_and_no_correlation_id(self, monkeypatch, caplog):
        with caplog.at_level(logging.WARNING, logger="pipeline"):
            _reload_with_env(monkeypatch, {}, unset=(*_TRANSPORT_VARS, _CORRELATION))
        assert _transport_warnings(caplog) == []

    def test_diagnostic_still_reports_transport_var_with_correlation_id(self):
        present = ignored_env_vars_present(
            {"PIPELINE_TRANSPORT_NUM_CTX": "1", _CORRELATION: "abc"}
        )
        assert [entry["name"] for entry in present] == ["PIPELINE_TRANSPORT_NUM_CTX"]
