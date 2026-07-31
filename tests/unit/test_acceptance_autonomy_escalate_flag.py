"""Acceptance oracle: PIPELINE_AUTO_ESCALATE must gate escalation independently
of PIPELINE_BACKEND_DISPATCH.

Grades the real gate object the server call sites use (not a copy), plus the
full truth table including the unset/back-compat case.
"""
import pipeline.escalation as esc
import pipeline.server as srv


def test_server_call_sites_use_the_escalation_module_gate():
    assert srv._auto_escalation_enabled is esc._auto_escalation_enabled


def test_explicit_opt_in_overrides_local_dispatch(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setenv("PIPELINE_AUTO_ESCALATE", "1")
    assert esc._auto_escalation_enabled() is True


def test_explicit_opt_in_accepts_word_forms(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    for value in ("true", "TRUE", "yes", "on"):
        monkeypatch.setenv("PIPELINE_AUTO_ESCALATE", value)
        assert esc._auto_escalation_enabled() is True, value


def test_explicit_opt_out_overrides_auto_dispatch(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setenv("PIPELINE_AUTO_ESCALATE", "0")
    assert esc._auto_escalation_enabled() is False


def test_unset_preserves_dispatch_mode_behavior(monkeypatch):
    monkeypatch.delenv("PIPELINE_AUTO_ESCALATE", raising=False)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    assert esc._auto_escalation_enabled() is True
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    assert esc._auto_escalation_enabled() is False


def test_empty_value_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setenv("PIPELINE_AUTO_ESCALATE", "   ")
    assert esc._auto_escalation_enabled() is True
