"""ENF-01: the story-state write tools are gated by ``pipeline.authorization``.

docs/specs/IDENTITY_AND_POLICY.md section 4 listed ``set_story_status``,
``mark_story_in_progress`` and ``checkpoint``/``checkpoint_story`` among the
mutating tools whose service path never asked the policy. Under
``PIPELINE_IDENTITY_PROVIDER=oidc`` any authenticated dashboard caller could
rewrite a story's status -- forcing ``done`` and skipping review and merge --
or write into its checkpoint journal. ADR 0009 puts the decision in
``PipelineService`` so no adapter (dashboard, chat, MCP) can route around it.

The action mapping is the one ADR 0009 section 5 fixes: actions map one-to-one
onto the existing ``app.policy.Action`` enum, so nothing new is invented here.

* ``set_story_status`` -> ``story:patch``
* ``mark_story_in_progress`` -> ``plan:dispatch`` (claiming a story is the
  first step of dispatch)
* ``checkpoint`` (the MCP ``checkpoint_story`` and the deprecated
  ``checkpoint`` both land here) -> ``story:patch``, with
  ``record_allowed=False``

That last flag is the one behavioural asymmetry these tests pin: ``checkpoint``
runs on every agent tick, so an ``allowed`` audit record per call would bury
the chain. Its denials are still fully recorded.

The contract:

* the unbound local operator -- what the scheduler and daemon run as -- still
  performs all three, with unchanged return shapes;
* an allowed ``set_story_status`` appends exactly one ``allowed`` record naming
  ``story:patch``; an allowed ``checkpoint`` appends none;
* a bound non-operator principal is refused by all three, the manifest bytes
  are unchanged (for ``checkpoint`` the journal is untouched and no WIP commit
  is made), and each denial appends exactly one ``authorization_denied``
  sidecar record and one ``denied`` audit record;
* ``PIPELINE_POLICY_ENFORCE=0`` allows all three;
* ``_validate_key`` runs FIRST, so a traversal key is refused with no sidecar
  record and no manifest read;
* ``authorize(..., record_allowed=False)`` still audits a DENIED call;
* the pre-existing invalid-status rejection survives for the local operator;
* through the dashboard, a refusal answers 403 ``not authorized: <action>``
  with no principal in the body, while an ordinary failure keeps its status.

``plan_dir`` mirrors the fixture in tests/unit/test_patch_plan_enforcement.py:
a per-test ``tmp_path`` with the ``pipeline.server``, ``persistence``,
``concurrency`` and ``checkpoint``-visible PLAN_DIR bindings redirected.
"""

from __future__ import annotations

import json

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline.actor import ANONYMOUS, bind_principal
from pipeline.authorization import ENFORCE_ENV, authorize

PLAN = "demo"
STORY = "S"
DENIAL_EVENT = "authorization_denied"
ACTION_STATUS = "story:patch"
ACTION_CLAIM = "plan:dispatch"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point every PLAN_DIR binding at a throwaway directory."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", directory)
    monkeypatch.setattr(ppers, "PLAN_DIR", directory)
    monkeypatch.setattr(pcon, "PLAN_DIR", directory)
    return directory


def _manifest(**overrides):
    manifest = {
        "repo_root": "/tmp/repo",
        "version": 3,
        "role_config": {"default": "sonnet"},
        "stories": {
            STORY: {
                "id": STORY,
                "summary": "demo story",
                "status": "todo",
                "dependencies": [],
                "worktree": "/tmp/worktree",
            }
        },
    }
    manifest.update(overrides)
    return manifest


def _write_plan(plan_dir, **overrides):
    path = plan_dir / f"{PLAN}.manifest.json"
    path.write_text(json.dumps(_manifest(**overrides), indent=2))
    return path


@pytest.fixture
def no_wip_commits(monkeypatch):
    """Stub the git-side WIP commit ``checkpoint`` performs.

    Returns the list of calls so a test can assert that a refusal never
    reached git at all.
    """
    from pipeline import checkpoint as pcheckk

    calls: list[tuple] = []

    def _fake_commit(worktree, story_key, step, *args, **kwargs):
        calls.append((worktree, story_key, step))
        return "0" * 40

    monkeypatch.setattr(pcheckk, "_commit_wip", _fake_commit)
    return calls


def _journal_path(plan_dir):
    return plan_dir / f"{PLAN}.{STORY}.journal.json"


def _audit_records(plan_dir, plan_name=PLAN):
    """Every record in the plan's audit stream, or ``[]`` when absent."""
    path = plan_dir / f"{plan_name}.audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _denials(plan_dir, plan_name=PLAN):
    """Every ``authorization_denied`` record in the plan's JSONL sidecar."""
    path = plan_dir / f"{plan_name}.notifications.jsonl"
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in records if r.get("event") == DENIAL_EVENT]


def _service():
    return p.PipelineService()


# --------------------------------------------------------------------------- #
# Positive: the local operator keeps working, unchanged shapes
# --------------------------------------------------------------------------- #
def test_local_operator_can_still_set_status(plan_dir, monkeypatch) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    path = _write_plan(plan_dir)

    with bind_principal(None):
        result = _service().set_story_status(PLAN, STORY, "parked")

    assert result == {"ok": True, "story_key": STORY, "status": "parked"}
    assert json.loads(path.read_text())["stories"][STORY]["status"] == "parked"


def test_local_operator_can_still_mark_in_progress(plan_dir, monkeypatch) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    path = _write_plan(plan_dir)

    with bind_principal(None):
        result = _service().mark_story_in_progress(PLAN, STORY)

    assert result == {"ok": True}
    assert json.loads(path.read_text())["stories"][STORY]["status"] == "in_progress"


def test_local_operator_can_still_checkpoint(plan_dir, monkeypatch, no_wip_commits) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir)

    with bind_principal(None):
        result = _service().checkpoint(PLAN, STORY, "s1", "did a thing", "keep going")

    assert result["ok"] is True
    assert result["step"] == "s1"
    assert result["summary"] == "did a thing"
    assert result["next_hint"] == "keep going"
    assert json.loads(_journal_path(plan_dir).read_text())[0]["step"] == "s1"


def test_allowed_status_change_appends_one_allowed_audit_record(
    plan_dir, monkeypatch
) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir)

    with bind_principal(None):
        _service().set_story_status(PLAN, STORY, "parked")

    records = _audit_records(plan_dir)
    assert [r["action"] for r in records] == [ACTION_STATUS]
    assert records[0]["outcome"] == "allowed"


def test_allowed_checkpoint_appends_no_audit_record(
    plan_dir, monkeypatch, no_wip_commits
) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir)

    with bind_principal(None):
        _service().checkpoint(PLAN, STORY, "s1", "summary")

    assert _audit_records(plan_dir) == []


# --------------------------------------------------------------------------- #
# Negative: a bound non-operator principal is refused
# --------------------------------------------------------------------------- #
def test_denied_set_status_names_action_and_principal(plan_dir, monkeypatch) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    path = _write_plan(plan_dir)
    before = path.read_bytes()

    with bind_principal(ANONYMOUS):
        result = _service().set_story_status(PLAN, STORY, "done")

    assert result["ok"] is False
    assert ACTION_STATUS in result["error"] and ANONYMOUS.subject in result["error"]
    assert path.read_bytes() == before


def test_denied_mark_in_progress_names_action_and_principal(plan_dir, monkeypatch) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    path = _write_plan(plan_dir)
    before = path.read_bytes()

    with bind_principal(ANONYMOUS):
        result = _service().mark_story_in_progress(PLAN, STORY)

    assert result["ok"] is False
    assert ACTION_CLAIM in result["error"] and ANONYMOUS.subject in result["error"]
    assert path.read_bytes() == before


def test_denied_checkpoint_leaves_journal_and_git_untouched(
    plan_dir, monkeypatch, no_wip_commits
) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    path = _write_plan(plan_dir)
    before = path.read_bytes()

    with bind_principal(ANONYMOUS):
        result = _service().checkpoint(PLAN, STORY, "s1", "summary")

    assert result["ok"] is False
    assert ACTION_STATUS in result["error"] and ANONYMOUS.subject in result["error"]
    assert path.read_bytes() == before
    assert not _journal_path(plan_dir).exists()
    assert no_wip_commits == []


@pytest.mark.parametrize(
    "call,action",
    [
        (lambda svc: svc.set_story_status(PLAN, STORY, "parked"), ACTION_STATUS),
        (lambda svc: svc.mark_story_in_progress(PLAN, STORY), ACTION_CLAIM),
        (lambda svc: svc.checkpoint(PLAN, STORY, "s1", "summary"), ACTION_STATUS),
    ],
    ids=["set-status", "mark-in-progress", "checkpoint"],
)
def test_denial_appends_one_sidecar_record_and_one_audit_record(
    plan_dir, monkeypatch, no_wip_commits, call, action
) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir)

    with bind_principal(ANONYMOUS):
        call(_service())

    denials = _denials(plan_dir)
    assert len(denials) == 1 and action in json.dumps(denials[0])
    outcomes = [(r["action"], r["outcome"]) for r in _audit_records(plan_dir)]
    assert outcomes == [(action, "denied")]


# --------------------------------------------------------------------------- #
# The kill switch, traversal ordering, and record_allowed on a denial
# --------------------------------------------------------------------------- #
def test_enforcement_disabled_allows_all_three(plan_dir, monkeypatch, no_wip_commits) -> None:
    monkeypatch.setenv(ENFORCE_ENV, "0")
    _write_plan(plan_dir)

    with bind_principal(ANONYMOUS):
        status = _service().set_story_status(PLAN, STORY, "parked")
        claim = _service().mark_story_in_progress(PLAN, STORY)
        checkpoint = _service().checkpoint(PLAN, STORY, "s1", "summary")

    assert status == {"ok": True, "story_key": STORY, "status": "parked"}
    assert claim == {"ok": True}
    assert checkpoint["ok"] is True


def test_traversal_is_refused_before_the_gate_runs(plan_dir, monkeypatch) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir)

    with bind_principal(ANONYMOUS):
        for call in (
            lambda: _service().set_story_status("../escape", STORY, "done"),
            lambda: _service().mark_story_in_progress("../escape", STORY),
            lambda: _service().checkpoint("../escape", STORY, "s1", "summary"),
        ):
            with pytest.raises(ValueError, match="invalid plan/story key"):
                call()

    assert _denials(plan_dir, "../escape") == []


def test_record_allowed_false_still_audits_a_denial(plan_dir, monkeypatch) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir)

    with bind_principal(ANONYMOUS):
        refusal = authorize(
            ACTION_STATUS, plan_name=PLAN, story_key=STORY, record_allowed=False
        )

    assert refusal is not None and refusal["ok"] is False
    assert [(r["action"], r["outcome"]) for r in _audit_records(plan_dir)] == [
        (ACTION_STATUS, "denied")
    ]
    assert len(_denials(plan_dir)) == 1


def test_local_operator_still_gets_the_invalid_status_rejection(
    plan_dir, monkeypatch
) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir)

    with bind_principal(None):
        result = _service().set_story_status(PLAN, STORY, "not-a-status")

    assert result["ok"] is False and "invalid status" in result["error"]


# --------------------------------------------------------------------------- #
# Dashboard: a refusal answers 403, never the route's ordinary 400/404
# --------------------------------------------------------------------------- #
_HEADER = "X-Pipeline-Api-Key"
_REFUSED_PRINCIPAL = "alice"


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Point app.auth at a throwaway key file; never touch the real one."""
    from app import auth

    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return path


class _FixedPrincipalProvider:
    """An identity provider that authenticates every credential as one fixed
    principal -- the pattern tests/unit/test_dashboard_refusal_routes.py uses."""

    def __init__(self, principal):
        self._principal = principal

    def authenticate(self, credential):
        return self._principal


@pytest.fixture
def alice_key_file(key_file, monkeypatch):
    """A usable dashboard key and every request authenticated as alice."""
    from app import auth
    from app import dashboard as d
    from app.identity import Principal

    auth.get_or_create_api_key()
    provider = _FixedPrincipalProvider(
        Principal(subject=_REFUSED_PRINCIPAL, auth_method="oidc")
    )
    monkeypatch.setattr(d, "_identity", provider)
    return key_file


def _post(path, payload=None):
    from fastapi.testclient import TestClient

    from app import auth
    from app import dashboard as d

    client = TestClient(d.app, headers={_HEADER: auth.get_or_create_api_key()})
    return client.post(path, json=payload) if payload is not None else client.post(path)


_DASHBOARD_CALLS = [
    pytest.param(
        f"/api/plans/{PLAN}/stories/{STORY}/status",
        {"status": "done"},
        ACTION_STATUS,
        id="set-status",
    ),
    pytest.param(f"/api/plans/{PLAN}/stories/{STORY}/start", None, ACTION_CLAIM, id="start"),
    pytest.param(
        f"/api/plans/{PLAN}/stories/{STORY}/checkpoint",
        {"step": "s1", "summary": "summary"},
        ACTION_STATUS,
        id="checkpoint",
    ),
]


@pytest.mark.parametrize("path,payload,action", _DASHBOARD_CALLS)
def test_dashboard_refusal_answers_403(
    plan_dir, alice_key_file, monkeypatch, no_wip_commits, path, payload, action
) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir)

    response = _post(path, payload)

    assert response.status_code == 403
    assert response.json()["detail"] == f"not authorized: {action}"
    assert _REFUSED_PRINCIPAL not in response.text


def test_dashboard_unknown_story_still_404_for_start(
    plan_dir, key_file, monkeypatch, no_wip_commits
) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir)

    response = _post(f"/api/plans/{PLAN}/stories/missing/start")

    assert response.status_code == 404
    assert response.json()["detail"] == "No such story missing"


def test_dashboard_ordinary_failure_keeps_its_status(
    plan_dir, key_file, monkeypatch
) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir)

    response = _post(
        f"/api/plans/{PLAN}/stories/{STORY}/checkpoint", {"step": "s1"}
    )

    assert response.status_code == 400
    assert response.json()["detail"].startswith("Both 'step' and 'summary'")

