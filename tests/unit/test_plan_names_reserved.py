"""Reserve the plan name ``host`` (audit anchor, Known gap (i)).

The audit anchor log keys the *host chain* as ``host``
(``pipeline/audit_anchor.py`` ``HOST_CHAIN = "host"``), so a plan that shares
that name would make the anchor log ambiguous. ``save_plan``, ``ingest_plan``
and the record store therefore refuse it, before anything is written or
authorized.
"""

from __future__ import annotations

import json

import pytest

import pipeline.persistence as ppers
import pipeline.server as p
from pipeline import authorization, ingest, plan_names

VALID_PLAN = {"epics": [{"summary": "E1", "stories": [{"key": "s1"}]}]}
RESERVED_MESSAGE = (
    'plan name "host" is reserved: the audit log uses it for the host chain'
)


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Mirror of the plan_dir fixture in test_save_plan_migration.py."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    return d


def test_reserved_plan_name_error_is_a_value_error():
    """Callers that catch ``ValueError`` keep working."""
    assert issubclass(plan_names.ReservedPlanNameError, ValueError)


def test_save_plan_rejects_the_reserved_name(plan_dir):
    """``save_plan("host")`` dies before writing anything at all."""
    with pytest.raises(plan_names.ReservedPlanNameError) as excinfo:
        p.save_plan("host", json.dumps(VALID_PLAN))
    assert str(excinfo.value) == RESERVED_MESSAGE
    names = {path.name for path in plan_dir.iterdir()}
    assert "host.json" not in names
    assert "host.audit.jsonl" not in names
    assert ".audit.host.jsonl" not in names
    assert list(plan_dir.iterdir()) == []


def test_ingest_plan_rejects_the_reserved_name_before_authorize(plan_dir, monkeypatch):
    """A refused name must never reach ``authorize``."""
    calls: list[tuple] = []

    def _sentinel(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(authorization, "authorize", _sentinel)
    with pytest.raises(plan_names.ReservedPlanNameError) as excinfo:
        p.ingest_plan("host")
    assert str(excinfo.value) == RESERVED_MESSAGE
    assert calls == []


def test_ingest_plan_impl_rejects_the_reserved_name(plan_dir):
    """Defense in depth: the impl refuses even if a caller skips the service."""
    with pytest.raises(plan_names.ReservedPlanNameError):
        ingest._ingest_plan_impl("host")


def test_save_plan_accepts_a_near_name(plan_dir):
    """The reservation is exact: ``hostname`` is still a valid plan name."""
    result = p.save_plan("hostname", json.dumps(VALID_PLAN))
    assert result["ok"] is True
    assert (plan_dir / "hostname.json").exists()


def test_save_plan_still_rejects_traversal_first(plan_dir):
    """A traversal name dies in the key check, not the reservation."""
    with pytest.raises(ValueError) as excinfo:
        p.save_plan("../x", json.dumps(VALID_PLAN))
    assert not isinstance(excinfo.value, plan_names.ReservedPlanNameError)


def test_reference_md_documents_the_reservation():
    """The user-facing reference states the reservation in one sentence."""
    import pathlib

    reference = pathlib.Path(p.__file__).resolve().parents[1] / "REFERENCE.md"
    text = reference.read_text()
    assert (
        "The plan name `host` is reserved: `save_plan`, `ingest_plan` and the "
        "record store refuse it, because the audit anchor log keys the host "
        "chain as `host`."
    ) in text
