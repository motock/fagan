"""AUTH-04: ``mark_story_done`` must be authorized before it takes the lock.

``_mark_story_done_impl`` closes a story on the manifest and in the ticket
backend. It is reachable from the dashboard and chat adapters as well as from
the scheduler, so an unauthenticated caller must not be able to mark work
done. The refusal has to happen *before* ``_store.transaction(plan_name)`` is
entered: a refused caller must not take the plan lock and must never reach
``get_ticket_provider().set_state(...)``, which runs inside it.

The function is rebound onto ``pipeline.server``'s namespace, so its bare-name
reads of ``_store`` / ``get_ticket_provider`` resolve against the live
``pipeline.server`` module -- which is what these tests patch.
"""

import json

import pytest

import pipeline.server as p
from app.identity import LOCAL_OPERATOR
from pipeline import ci as pci
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import plan_completion as pcomp
from pipeline.actor import ANONYMOUS, bind_principal
from pipeline.authorization import ENFORCE_ENV
from pipeline.ticketing import LogicalState

PLAN = "p"
STORY = "S1"
OTHER_STORY = "S2"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Redirect every PLAN_DIR copy to an isolated tmp dir."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    # plan_completion binds its own PLAN_DIR; unpatched, the completion report
    # would land in the real plan directory.
    monkeypatch.setattr(pcomp, "PLAN_DIR", d)
    return d


class RecordingProvider:
    """Ticket provider stub that records every ``set_state`` call."""

    def __init__(self):
        self.calls = []

    def set_state(self, story_key, state, plan_name=None):
        self.calls.append((story_key, state))


@pytest.fixture
def provider(monkeypatch):
    prov = RecordingProvider()
    monkeypatch.setattr(p, "get_ticket_provider", lambda: prov)
    return prov


def _write_manifest(plan_dir, stories):
    (plan_dir / f"{PLAN}.manifest.json").write_text(
        json.dumps({"repo_root": "/nonexistent-repo", "stories": stories})
    )


def _status(plan_dir, story_key=STORY):
    manifest = json.loads((plan_dir / f"{PLAN}.manifest.json").read_text())
    return manifest["stories"][story_key]["status"]


def _denial_records(plan_dir):
    path = plan_dir / f"{PLAN}.notifications.jsonl"
    if not path.exists():
        return []
    records = [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ]
    return [r for r in records if r.get("event") == "authorization_denied"]


@pytest.fixture(autouse=True)
def _enforcement_on(monkeypatch):
    """Every test starts with enforcement on unless it opts out explicitly."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)


# ---------------------------------------------------------------------------
# Anonymous caller: refused, nothing mutated, provider never called.
# ---------------------------------------------------------------------------


def test_anonymous_refused(plan_dir, provider):
    _write_manifest(plan_dir, {STORY: {"summary": "s", "status": "tests_passed"}})

    with bind_principal(ANONYMOUS):
        refusal = pci._mark_story_done_impl(PLAN, STORY)

    assert refusal["ok"] is False
    assert "story:mark_done" in repr(refusal)
    assert _status(plan_dir) == "tests_passed"
    assert provider.calls == []


def test_refusal_does_not_take_the_lock(plan_dir, provider, monkeypatch):
    """A refused caller must not enter the plan transaction at all."""
    _write_manifest(plan_dir, {STORY: {"summary": "s", "status": "tests_passed"}})
    entered = []

    from contextlib import contextmanager

    @contextmanager
    def _spy_transaction(name):
        entered.append(name)
        yield True

    monkeypatch.setattr(p._store, "transaction", _spy_transaction)

    with bind_principal(ANONYMOUS):
        refusal = pci._mark_story_done_impl(PLAN, STORY)

    assert refusal["ok"] is False
    assert entered == []


def test_refusal_then_allowed_call_records_exactly_one_provider_call(
    plan_dir, provider
):
    """The refusal must not have mutated state behind the returned dict."""
    _write_manifest(plan_dir, {STORY: {"summary": "s", "status": "tests_passed"}})

    with bind_principal(ANONYMOUS):
        refusal = pci._mark_story_done_impl(PLAN, STORY)
    assert refusal["ok"] is False
    assert _status(plan_dir) == "tests_passed"
    assert provider.calls == []

    with bind_principal(LOCAL_OPERATOR):
        result = pci._mark_story_done_impl(PLAN, STORY)

    assert result["ok"] is True
    assert _status(plan_dir) == "done"
    assert provider.calls == [(STORY, LogicalState.DONE)]


# ---------------------------------------------------------------------------
# Local operator (and no transport at all): allowed.
# ---------------------------------------------------------------------------


def test_local_operator_allowed(plan_dir, provider):
    _write_manifest(plan_dir, {STORY: {"summary": "s", "status": "tests_passed"}})

    with bind_principal(LOCAL_OPERATOR):
        result = pci._mark_story_done_impl(PLAN, STORY)

    assert result["ok"] is True
    assert _status(plan_dir) == "done"
    assert len(provider.calls) == 1
    assert provider.calls[0][0] == STORY


def test_nothing_bound_is_allowed(plan_dir, provider):
    """No transport at all means the local operator, not anonymous."""
    _write_manifest(plan_dir, {STORY: {"summary": "s", "status": "tests_passed"}})

    result = pci._mark_story_done_impl(PLAN, STORY)

    assert result["ok"] is True
    assert _status(plan_dir) == "done"
    assert len(provider.calls) == 1


# ---------------------------------------------------------------------------
# Kill switch.
# ---------------------------------------------------------------------------


def test_kill_switch(plan_dir, provider, monkeypatch):
    monkeypatch.setenv(ENFORCE_ENV, "0")
    _write_manifest(plan_dir, {STORY: {"summary": "s", "status": "tests_passed"}})

    with bind_principal(ANONYMOUS):
        result = pci._mark_story_done_impl(PLAN, STORY)

    assert result["ok"] is True
    assert _status(plan_dir) == "done"
    assert provider.calls == [(STORY, LogicalState.DONE)]


def test_kill_switch_is_read_on_every_call(plan_dir, provider, monkeypatch):
    """Re-enabling enforcement must refuse the very next call."""
    monkeypatch.setenv(ENFORCE_ENV, "0")
    _write_manifest(
        plan_dir,
        {
            STORY: {"summary": "s", "status": "tests_passed"},
            OTHER_STORY: {"summary": "s", "status": "tests_passed"},
        },
    )

    with bind_principal(ANONYMOUS):
        first = pci._mark_story_done_impl(PLAN, STORY)
    assert first["ok"] is True
    assert provider.calls == [(STORY, LogicalState.DONE)]

    monkeypatch.setenv(ENFORCE_ENV, "1")
    with bind_principal(ANONYMOUS):
        second = pci._mark_story_done_impl(PLAN, OTHER_STORY)

    assert second["ok"] is False
    assert _status(plan_dir, OTHER_STORY) == "tests_passed"
    assert len(provider.calls) == 1


# ---------------------------------------------------------------------------
# The denial is recorded in the plan's sidecar.
# ---------------------------------------------------------------------------


def test_denial_sidecar(plan_dir, provider):
    _write_manifest(plan_dir, {STORY: {"summary": "s", "status": "tests_passed"}})

    with bind_principal(ANONYMOUS):
        pci._mark_story_done_impl(PLAN, STORY)

    records = _denial_records(plan_dir)
    assert len(records) == 1
    record = records[0]
    assert record["severity"] == "warning"
    assert record["story_key"] == STORY
    assert "story:mark_done" in record["message"]


def test_allowed_call_records_no_denial(plan_dir, provider):
    _write_manifest(plan_dir, {STORY: {"summary": "s", "status": "tests_passed"}})

    with bind_principal(LOCAL_OPERATOR):
        pci._mark_story_done_impl(PLAN, STORY)

    assert _denial_records(plan_dir) == []
