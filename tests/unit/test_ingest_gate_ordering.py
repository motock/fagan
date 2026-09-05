"""Regression tests for the ORDER and SCOPE of the ingest-time
acceptance-fixture block gate (reviewer blocking findings 1 and 1b).

Finding (1) -- mutate-then-check: today the fail-closed gate sits AFTER the
ticket-provider loop in ``pipeline/ingest.py``, so a blocked ingest still
drives the provider (a real PlaneTicketProvider fires real ``plane_request``
POSTs in create_epic/create_story) and only then returns ``ok:False``
without writing the manifest -- orphaning/duplicating Plane tickets on
retry. The discriminator below is the recorded provider call count, not the
manifest assertion (the manifest is already absent today; the spy count is
what catches the ordering bug).

Finding (1b) -- wrong iteration source: the gate iterates
``final_manifest["stories"]`` (prior manifest merged with new), so findings
stored on already-dispatched stories from a prior ingest veto an unrelated
``only_epics`` re-ingest. After the fix the gate must validate only the
current (only_epics-filtered) plan's stories.

Both tests inject a recording stub ticket provider exactly the way the
existing suite injects providers (monkeypatching the live
``pipeline.server.get_ticket_provider`` binding that ``pipeline.ingest``
resolves through ``_ServerRef`` -- see test_mark_story_in_progress_migration),
and enable blocking mode via the ``PIPELINE_ACCEPTANCE_FIXTURE_VALIDATE_BLOCK``
env flag whose unset/empty/case/"0" parsing this change introduced.

Fixture mechanics (verified against pipeline/build_detect.py): both
validators materialize the story's *stored* acceptance ``source`` dicts into
a temp dir; the lint validator runs ruff (skipped entirely when ruff is not
on PATH) and the pytest validator runs ``pytest --collect-only -q``. So:

* a guaranteed, ruff-independent "finding" is a fixture that fails
  *collection* (module-level ``raise``) -- NOT ``def test_...(): assert
  False``, which collects cleanly because collect-only never executes test
  bodies;
* a "clean" fixture must be a collectable ``test_*.py`` file (a non-test
  filename collects zero tests and pytest exits nonzero).
"""
import json

import pytest

from pipeline import server as p

BLOCK_FLAG = "PIPELINE_ACCEPTANCE_FIXTURE_VALIDATE_BLOCK"

# Collects fine -> pytest validator "clean" (and lint-clean for ruff).
CLEAN_SOURCE = "def test_gate():\n    assert True\n"
# Fails *collection* (module-level raise) -> pytest validator "finding",
# independent of whether ruff is installed.
BROKEN_SOURCE = 'raise RuntimeError("fixture is collection-broken")\n'


class RecordingTicketProvider:
    """Same create_epic/create_story surface as the real providers, but every
    call is appended to ``calls`` so tests can assert the provider loop never
    ran for a blocked ingest. Returns string ids because ingest uses them as
    manifest story keys."""

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


def _story(summary, key, fixture_name, source):
    return {
        "summary": summary,
        "key": key,
        "agent_instructions": "do the thing",
        "acceptance": [{"path": f"tests/{fixture_name}", "source": source}],
    }


def _write_plan(plan_dir, name, plan):
    (plan_dir / f"{name}.json").write_text(json.dumps(plan))


def test_blocked_ingest_never_touches_ticket_provider(
    plan_dir, tmp_path, monkeypatch, stub_provider
):
    """A blocked ingest must fail closed BEFORE the provider loop runs.

    Correct: gate first -> S1 "clean", S2 "finding" -> ok:False with S2's
    finding, ZERO provider calls, manifest untouched. Buggy (today): provider
    loop first -> create_epic + create_story x2 fire, then the gate sees S2's
    finding -> ok:False with no manifest, leaving orphaned tickets.
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
                    _story("S1 clean", "S1", "test_acceptance_ok.py", CLEAN_SOURCE),
                    _story(
                        "S2 broken", "S2", "test_acceptance_broken.py", BROKEN_SOURCE
                    ),
                ],
            }
        ],
    }
    _write_plan(plan_dir, "gateorder", plan)

    result = p.ingest_plan("gateorder")

    assert result.get("ok") is False, result
    assert "S2 broken" in result.get("error", ""), result
    assert "test_acceptance_broken.py" in result.get("error", ""), result
    assert stub_provider.calls == [], (
        "blocked ingest must run the acceptance-fixture gate BEFORE the "
        f"ticket-provider loop; provider recorded {stub_provider.calls}"
    )
    assert not (plan_dir / "gateorder.manifest.json").exists()


def test_stale_prior_manifest_finding_does_not_veto_only_epics_reingest(
    plan_dir, tmp_path, monkeypatch, stub_provider
):
    """Finding 1b: the gate must not iterate the merged
    ``final_manifest["stories"]`` -- findings stored on already-dispatched
    stories from a prior ingest must not veto an unrelated only_epics
    re-ingest.

    Ingest #1 runs in advisory mode (block flag unset) with story A carrying
    a finding: A is dispatched anyway and its finding-level acceptance is
    stored in the manifest. Ingest #2 then re-ingests only epic E2 (clean
    stories) in blocking mode: the stale A finding from the prior manifest
    must not refuse it.

    Buggy (today): the gate iterates final_manifest["stories"] = {A, B},
    hits A's stored finding -> ok:False, E2 never created.
    """
    notified = []
    monkeypatch.setattr(p, "_notify_user", lambda *a, **kw: notified.append(a))

    plan = {
        "repo_root": str(tmp_path),
        "epics": [
            {
                "summary": "E1",
                "stories": [
                    _story("A broken", "A", "test_acceptance_a.py", BROKEN_SOURCE)
                ],
            },
            {
                "summary": "E2",
                "stories": [
                    _story("B clean", "B", "test_acceptance_b.py", CLEAN_SOURCE)
                ],
            },
        ],
    }
    _write_plan(plan_dir, "stalegate", plan)

    # Ingest #1: advisory mode -- A's finding is surfaced but never blocks.
    monkeypatch.delenv(BLOCK_FLAG, raising=False)
    first = p.ingest_plan("stalegate")
    assert first.get("ok") is True, first
    assert (plan_dir / "stalegate.manifest.json").exists()
    first_calls = list(stub_provider.calls)
    assert first_calls, "ingest #1 must reach the provider loop"
    assert any(
        "A broken" in str(args) for call in first_calls for args in call[1:3]
    ), first_calls

    # Ingest #2: blocking mode, only epic E2 -- A's stale stored finding
    # (already dispatched in ingest #1) must not veto it.
    monkeypatch.setenv(BLOCK_FLAG, "1")
    second = p.ingest_plan("stalegate", only_epics=["E2"])

    assert second.get("ok") is True, second
    new_calls = stub_provider.calls[len(first_calls):]
    assert any(call[0] == "create_epic" for call in new_calls), (
        "E2 must be created on the only_epics re-ingest; the stale finding on "
        f"already-dispatched story A must not veto it; new calls {new_calls}"
    )
    assert (plan_dir / "stalegate.manifest.json").exists()


def test_gate_validates_only_only_epics_filtered_plan_stories(
    plan_dir, tmp_path, monkeypatch, stub_provider
):
    """Scope guard for the relocated gate: it must validate only the current
    plan's only_epics-filtered stories -- a story belonging to an excluded
    epic must not be validated (and must not block) even when its acceptance
    source in the plan file is broken.

    Ingest #1 (blocking) dispatches clean A and B. The plan file is then
    edited so A's fixture is collection-broken. Ingest #2 (blocking,
    only_epics=["E2"]) must still succeed: the gate validates only E2's
    stories. A naive fix that iterates every plan story unfiltered fails
    here; the buggy pre-fix code passes (its gate reads the prior manifest,
    where A is still clean) -- this test pins the correct scope.
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
                    _story("A clean", "A", "test_acceptance_a.py", CLEAN_SOURCE)
                ],
            },
            {
                "summary": "E2",
                "stories": [
                    _story("B clean", "B", "test_acceptance_b.py", CLEAN_SOURCE)
                ],
            },
        ],
    }
    _write_plan(plan_dir, "scopegate", plan)

    first = p.ingest_plan("scopegate")
    assert first.get("ok") is True, first
    first_calls = list(stub_provider.calls)
    assert first_calls

    # Story A's acceptance source in the plan file goes stale (collection-
    # broken). Validators materialize stored acceptance sources, so this
    # only matters if the gate (wrongly) validates A on ingest #2.
    plan["epics"][0]["stories"][0]["acceptance"][0]["source"] = BROKEN_SOURCE
    _write_plan(plan_dir, "scopegate", plan)

    second = p.ingest_plan("scopegate", only_epics=["E2"])

    assert second.get("ok") is True, second
    new_calls = stub_provider.calls[len(first_calls):]
    assert any(call[0] == "create_epic" for call in new_calls), (
        f"E2 must be created; excluded epic E1's broken fixture must not be "
        f"validated; new calls {new_calls}"
    )