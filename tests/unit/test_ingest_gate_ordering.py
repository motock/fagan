"""Regression tests for the ORDER of the ingest-time acceptance-fixture
block gate (reviewer blocking finding 1 + 1b).

The fail-closed gate must run BEFORE the ticket-provider loop:

* Finding (1): today the gate sits after the provider loop, so a blocked
  ingest still drives the ticket provider (a real PlaneTicketProvider would
  fire real ``plane_request`` POSTs in create_epic/create_story) and only
  then returns ``ok:False`` without writing the manifest -- orphaning /
  duplicating Plane tickets on retry. The discriminator here is the recorded
  provider call count, not the manifest assertion (the manifest is already
  absent today; the spy count is what catches the mutate-then-check bug).
* Finding (1b): the gate iterates ``final_manifest["stories"]`` (prior
  manifest merged with new), so a stale finding on an already-dispatched
  story from a prior ingest vetoes an unrelated ``only_epics`` re-ingest.

Both tests inject a recording stub ticket provider exactly the way the
existing suite injects NullTicketProvider (monkeypatching the live
``pipeline.server.get_ticket_provider`` binding that ``pipeline.ingest``
resolves through ``_ServerRef``), and enable blocking mode via the
``PIPELINE_ACCEPTANCE_FIXTURE_VALIDATE_BLOCK`` env flag whose
unset/empty/case/"0" parsing this change introduced.

The broken fixture is a real pytest file whose test fails, so the finding
does not depend on ruff being installed (per the review's "ruff not found"
note the lint validator may be skipped entirely).
"""
import json

import pytest

from pipeline import server as p

BLOCK_FLAG = "PIPELINE_ACCEPTANCE_FIXTURE_VALIDATE_BLOCK"

CLEAN_SOURCE = "def test_gate():\n    assert True\n"
FAILING_SOURCE = "def test_gate():\n    assert False\n"


class RecordingTicketProvider:
    """Same create_epic/create_story surface as the real providers, but every
    call is appended to ``calls`` so tests can assert the provider loop never
    ran for a blocked ingest."""

    def __init__(self):
        self.calls = []

    def create_epic(self, *args, **kwargs):
        self.calls.append(("create_epic", args, kwargs))
        return "rec-epic"

    def create_story(self, *args, **kwargs):
        self.calls.append(("create_story", args, kwargs))
        return "rec-story"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


@pytest.fixture
def stub_provider(monkeypatch):
    stub = RecordingTicketProvider()
    monkeypatch.setattr(p, "get_ticket_provider", lambda: stub)
    return stub


def _story(summary, fixture_name, source):
    return {
        "summary": summary,
        "agent_instructions": "do the thing",
        "acceptance": [{"path": f"tests/{fixture_name}", "source": source}],
    }


def _write_plan(plan_dir, name, plan):
    (plan_dir / f"{name}.json").write_text(json.dumps(plan))


def test_blocked_ingest_never_touches_ticket_provider(
    plan_dir, tmp_path, monkeypatch, stub_provider
):
    """A blocked ingest must fail closed BEFORE the provider loop runs.

    Correct: gate first -> S2 'finding' -> ok:False, zero provider calls,
    manifest untouched. Buggy (today): provider loop first -> create_epic +
    create_story x2 fire, then ok:False with no manifest.
    """
    monkeypatch.setenv(BLOCK_FLAG, "1")
    notified = []
    monkeypatch.setattr(p, "_notify_user", lambda *a, **kw: notified.append(a))

    plan = {
        "repo_root": str(tmp_path),
        "epics": [
            {
                "summary": "E1",
                "stories": [
                    _story("S1 clean", "acceptance_clean.py", CLEAN_SOURCE),
                    _story("S2 broken", "acceptance_broken.py", FAILING_SOURCE),
                ],
            }
        ],
    }
    _write_plan(plan_dir, "gateorder", plan)

    result = p.ingest_plan("gateorder")

    assert result.get("ok") is False, result
    assert "S2 broken" in result.get("error", ""), result
    assert stub_provider.calls == [], (
        "blocked ingest must run the acceptance-fixture gate BEFORE the "
        f"ticket-provider loop; provider recorded {stub_provider.calls}"
    )
    assert not (plan_dir / "gateorder.manifest.json").exists()


def test_stale_finding_does_not_veto_only_epics_reingest(
    plan_dir, tmp_path, monkeypatch, stub_provider
):
    """An only_epics re-ingest must be gated on ITS OWN plan stories, not on
    the merged final_manifest["stories"] (finding 1b).

    Ingest #1 dispatches story A (clean). A's fixture then goes stale on
    disk. Ingest #2 re-ingests only epic E2 (clean stories): the stale A
    finding must not veto it.
    """
    monkeypatch.setenv(BLOCK_FLAG, "1")
    notified = []
    monkeypatch.setattr(p, "_notify_user", lambda *a, **kw: notified.append(a))

    plan = {
        "repo_root": str(tmp_path),
        "epics": [
            {
                "summary": "E1",
                "stories": [_story("A", "acceptance_a.py", CLEAN_SOURCE)],
            },
            {
                "summary": "E2",
                "stories": [_story("B", "acceptance_b.py", CLEAN_SOURCE)],
            },
        ],
    }
    _write_plan(plan_dir, "stalegate", plan)

    first = p.ingest_plan("stalegate")
    assert first.get("ok") is True, first
    assert (plan_dir / "stalegate.manifest.json").exists()
    first_calls = list(stub_provider.calls)
    assert first_calls, "ingest #1 must reach the provider loop"

    # Story A's acceptance fixture goes stale on disk after ingest #1.
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "acceptance_a.py").write_text(FAILING_SOURCE)

    second = p.ingest_plan("stalegate", only_epics=["E2"])

    assert second.get("ok") is True, second
    new_calls = stub_provider.calls[len(first_calls):]
    assert any(call[0] == "create_epic" for call in new_calls), (
        f"E2 must be created on the only_epics re-ingest; new calls {new_calls}"
    )
    assert (plan_dir / "stalegate.manifest.json").exists()