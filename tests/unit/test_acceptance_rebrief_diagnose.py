"""Acceptance oracle: diagnose_failure must turn failure evidence into a root
cause via a configurable role, and must FAIL OPEN (return None) on every error
path - a diagnosis is an optimization, never a gate.
"""
from pipeline import rebrief


def test_returns_the_role_output_as_the_diagnosis(monkeypatch):
    monkeypatch.setattr(
        rebrief, "_run_diagnosis_role", lambda *a, **k: "TEMPLATE_DIR came from --repo-root"
    )
    assert rebrief.diagnose_failure("evidence", {"summary": "s"}) == (
        "TEMPLATE_DIR came from --repo-root"
    )


def test_unconfigured_role_fails_open(monkeypatch):
    monkeypatch.setattr(rebrief, "_run_diagnosis_role", lambda *a, **k: None)
    assert rebrief.diagnose_failure("evidence", {"summary": "s"}) is None


def test_role_exception_fails_open(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("backend down")

    monkeypatch.setattr(rebrief, "_run_diagnosis_role", boom)
    assert rebrief.diagnose_failure("evidence", {"summary": "s"}) is None


def test_empty_role_output_fails_open(monkeypatch):
    monkeypatch.setattr(rebrief, "_run_diagnosis_role", lambda *a, **k: "   ")
    assert rebrief.diagnose_failure("evidence", {"summary": "s"}) is None


def test_empty_evidence_skips_the_role_entirely(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not call the role with no evidence")

    monkeypatch.setattr(rebrief, "_run_diagnosis_role", boom)
    assert rebrief.diagnose_failure("", {"summary": "s"}) is None
