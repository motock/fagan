"""approve_merge answers a refused merge with 409 and a fixed reason code.

Covers issue mrr-01:
  * app/dashboard_routes_story.py `approve_merge_route` answers 409 with
    `{"detail": {"reason", "message"}}` for every refusal the service reports,
    using the closed reason-code set derived from pipeline/merge.py.
  * The raw service error never reaches the response body.
  * A forbidden result and a chat-origin request are still refused unchanged.
"""
import pytest
from fastapi.testclient import TestClient

from app import dashboard as d

# (service error, reason code, fixed message). Each service error is the real
# prefix pipeline/merge.py emits, followed by detail that must never leak.
REFUSAL_CASES = [
    (
        "Story was never reviewer-approved",
        "not_reviewed",
        "The story was never approved by review.",
    ),
    (
        "rebase failed: CONFLICT in /srv/secret/app.py",
        "rebase_failed",
        "The branch could not be rebased onto the default branch.",
    ),
    (
        "CI failing: check verify failed on agent/mrr-01",
        "ci_failing",
        "CI is failing on the pull request.",
    ),
    (
        "CI still pending: verify queued on agent/mrr-01",
        "ci_pending",
        "CI is still running on the pull request.",
    ),
    (
        "ci unreadable: gh: 401 token expired",
        "ci_unreadable",
        "CI status could not be read.",
    ),
    (
        "acceptance reverify fail: 2 failed in tests/x.py",
        "acceptance_failed",
        "The acceptance checks failed after the rebase.",
    ),
    (
        "Story is done, not parked/pr_open",
        "not_mergeable",
        "The story cannot be merged in its current state.",
    ),
    (
        "build reverify fail: npm ERR! in /srv/secret/web",
        "not_mergeable",
        "The story cannot be merged in its current state.",
    ),
]


class _FakeService:
    """Stands in for the service seam; records each approve_merge call."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def approve_merge(self, plan_name, story_key):
        self.calls.append((plan_name, story_key))
        return dict(self.result)


@pytest.fixture
def client():
    return TestClient(d.app)


def _use_service(monkeypatch, result):
    fake = _FakeService(result)
    monkeypatch.setattr(d, "_service", fake)
    return fake


@pytest.mark.parametrize("error, reason, message", REFUSAL_CASES)
def test_refused_merge_answers_409_with_reason_and_fixed_message(
    client, monkeypatch, error, reason, message
):
    fake = _use_service(monkeypatch, {"ok": False, "error": error})

    res = client.post("/api/plans/demo/stories/S1/approve_merge")

    assert res.status_code == 409
    assert res.json() == {"detail": {"reason": reason, "message": message}}
    assert fake.calls == [("demo", "S1")]


@pytest.mark.parametrize("error, reason, message", REFUSAL_CASES)
def test_refused_merge_body_never_contains_the_raw_service_error(
    client, monkeypatch, error, reason, message
):
    _use_service(monkeypatch, {"ok": False, "error": error})

    res = client.post("/api/plans/demo/stories/S1/approve_merge")

    assert error not in res.text
    assert "/srv/secret" not in res.text


def test_busy_plan_answers_409_with_busy_reason(client, monkeypatch):
    _use_service(
        monkeypatch,
        {
            "ok": False,
            "error": "plan busy (scheduler tick in progress); retry",
            "retriable": True,
        },
    )

    res = client.post("/api/plans/demo/stories/S1/approve_merge")

    assert res.status_code == 409
    assert res.json() == {
        "detail": {
            "reason": "busy",
            "message": "The plan is busy; try again in a moment.",
        }
    }


def test_missing_story_still_answers_404(client, monkeypatch):
    _use_service(monkeypatch, {"ok": False, "error": "No such story NOPE"})

    res = client.post("/api/plans/demo/stories/NOPE/approve_merge")

    assert res.status_code == 404


def test_missing_plan_answers_404_with_fixed_detail(client, monkeypatch):
    fake = _FakeService({})

    def _raise_missing_plan(plan_name, story_key):
        raise FileNotFoundError(f"/srv/plans/{plan_name}.manifest.json")

    fake.approve_merge = _raise_missing_plan
    monkeypatch.setattr(d, "_service", fake)

    res = client.post("/api/plans/ghost/stories/S1/approve_merge")

    assert res.status_code == 404
    assert res.json() == {"detail": "Not found"}
    assert "/srv/plans" not in res.text


def test_forbidden_merge_still_answers_403_without_the_principal(client, monkeypatch):
    _use_service(
        monkeypatch,
        {
            "ok": False,
            "forbidden": True,
            "action": "story:merge",
            "error": "principal alice@corp.example denied",
        },
    )

    res = client.post("/api/plans/demo/stories/S1/approve_merge")

    assert res.status_code == 403
    assert res.json()["detail"] == "not authorized: story:merge"
    assert "alice" not in res.text


def test_chat_origin_is_refused_before_the_service_is_called(client, monkeypatch):
    fake = _use_service(monkeypatch, {"ok": True})

    res = client.post(
        "/api/plans/demo/stories/S1/approve_merge",
        headers={"X-Pipeline-Origin": "chat"},
    )

    assert res.status_code == 403
    assert fake.calls == []
