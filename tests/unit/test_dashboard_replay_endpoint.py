"""Tests for the dashboard's per-story replay endpoint.

Adds GET /api/plans/{plan_name}/stories/{story_key}/replay to
app/dashboard.py, inserted between get_story_checklist and the
@app.get("/api/config") decorator. The route mirrors get_story_journal's
documented contract:

  * 404 ONLY when the plan manifest is missing or the story_key is not in
    it (same two HTTPException blocks as get_story_journal);
  * everything else is 200 with
        {"available": bool, "events": [...], "sources": {...}}
    and NEVER 500 — a missing/corrupt journal, a story never dispatched
    (no worktree), a wiped agent.log are all normal available-degraded
    states;
  * journal read via _store.get_journal(plan_name, story_key)
    -> (available, entries);
  * worktree log tails via _store.get_worktree_file(story, "agent.log")
    and _store.get_worktree_file(story, "review.log") -> {"available",
    "text"}; the LAST N lines of each text are taken, N from a
    ``lines: int = 200`` query param clamped to [1, 500] (mirroring
    _LOG_TAIL_DEFAULT/_LOG_TAIL_CAP in app/dashboard_helpers.py);
  * events built by build_replay_events(journal_entries, log_sources)
    from app.story_replay, with log_sources built ONLY from available
    sources;
  * available is True iff the journal was available OR at least one log
    source was available;
  * "sources" echo {"journal": bool, "agent.log": bool, "review.log":
    bool} so the UI can label which inputs contributed.

These tests are RED until the route exists (TestClient gets a 404 from
Starlette because no /replay route is registered yet).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import dashboard as d
from tests.unit._dashboard_helpers import (  # noqa: F401
    _write_manifest,
    client,
    plan_dir,
)

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

JOURNAL_TS_A = "2025-01-01T10:00:00Z"
JOURNAL_TS_B = "2025-01-01T11:00:00Z"
LOG_TS_A = "2025-01-01T09:30:00Z"
LOG_TS_B = "2025-01-01T11:30:00Z"


@pytest.fixture
def worktree_dir(tmp_path, monkeypatch):
    """A throwaway WORKTREE_ROOT, mirroring the plan_dir fixture's
    monkeypatch of PLAN_DIR. The store resolves worktree free variables
    through pipeline.server, so BOTH module globals must be patched."""
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    monkeypatch.setattr(d, "WORKTREE_ROOT", wt_root)
    from pipeline import server as _srv

    monkeypatch.setattr(_srv, "WORKTREE_ROOT", wt_root)
    return wt_root


def _write_journal(plan_dir, plan_name, story_key, entries):
    (plan_dir / f"{plan_name}.{story_key}.journal.json").write_text(
        json.dumps(entries)
    )


def _write_worktree(worktree_dir, story_key, files):
    wt = worktree_dir / story_key
    wt.mkdir()
    for name, text in files.items():
        (wt / name).write_text(text)
    return wt


def _dispatched_story(worktree_dir, story_key="S1"):
    return {
        "summary": "dispatched",
        "status": "in_progress",
        "worktree": str(worktree_dir / story_key),
        "dependencies": [],
    }


def _undispatched_story():
    return {"summary": "never dispatched", "status": "todo", "dependencies": []}


def _get(client, plan="demo", story="S1", query=""):
    return client.get(f"/api/plans/{plan}/stories/{story}/replay{query}")


# ---------------------------------------------------------------------------
# route registration / placement / wiring (static assertions)
# ---------------------------------------------------------------------------


class TestRouteRegistration:
    """The brief pins WHERE the route lives and WHAT it delegates to."""

    def _source(self) -> str:
        return Path("app/dashboard.py").read_text(encoding="utf-8")

    def test_route_decorator_present(self):
        assert '@app.get("/api/plans/{plan_name}/stories/{story_key}/replay")' in (
            self._source()
        )

    def test_route_inserted_after_get_story_checklist(self):
        src = self._source()
        assert src.index("def get_story_checklist(") < src.index(
            "/api/plans/{plan_name}/stories/{story_key}/replay"
        )

    def test_route_inserted_before_api_config(self):
        src = self._source()
        assert src.index("/api/plans/{plan_name}/stories/{story_key}/replay") < (
            src.index('@app.get("/api/config")')
        )

    def test_route_before_static_mount(self):
        src = self._source()
        assert src.index("/api/plans/{plan_name}/stories/{story_key}/replay") < (
            src.index('app.mount("/",')
        )

    def test_imports_build_replay_events_at_module_top(self):
        src = self._source()
        assert "from app.story_replay import build_replay_events" in src

    def test_lines_param_defaults_to_200(self):
        src = self._source()
        assert "lines: int = 200" in src

    def test_docstring_written(self):
        """The route carries a docstring in get_story_journal's style
        (response shape, semantics, why 404 is reserved)."""
        fn = getattr(d, "get_story_replay", None)
        if fn is None:
            # Accept any handler name; find it via the FastAPI route table.
            fn = None
            for route in d.app.routes:
                if getattr(route, "path", "") == (
                    "/api/plans/{plan_name}/stories/{story_key}/replay"
                ):
                    fn = route.endpoint
                    break
        assert fn is not None, "no /replay route registered on d.app"
        doc = (fn.__doc__ or "").strip()
        assert doc, "replay route must have a docstring"
        lowered = doc.lower()
        assert "available" in lowered
        assert "404" in lowered
        assert "events" in lowered


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


class TestReplayHappyPath:
    def test_journal_plus_both_logs_merged_chronologically(
        self, client, plan_dir, worktree_dir
    ):
        _write_manifest(plan_dir, "demo", {"S1": _dispatched_story(worktree_dir)})
        _write_journal(
            plan_dir,
            "demo",
            "S1",
            [
                {
                    "step": "implement",
                    "summary": "wrote the parser",
                    "next_hint": "add tests",
                    "ts": JOURNAL_TS_B,
                },
                {
                    "step": "plan",
                    "summary": "read the brief",
                    "next_hint": None,
                    "ts": JOURNAL_TS_A,
                },
            ],
        )
        _write_worktree(
            worktree_dir,
            "S1",
            {
                "agent.log": (
                    f"{LOG_TS_A} started dispatch\n"
                    "plain continuation line\n"
                    f"{LOG_TS_B} finished dispatch\n"
                ),
                "review.log": f"{LOG_TS_B} review passed\n",
            },
        )

        res = _get(client)
        assert res.status_code == 200
        body = res.json()
        assert body["available"] is True
        events = body["events"]
        # 2 journal entries + 2 agent.log timestamped events + 1 review.log
        assert len(events) == 5
        # merged and sorted chronologically across sources
        timed = [e["ts"] for e in events]
        assert timed == sorted(timed)
        assert [e["source"] for e in events] == [
            "agent.log",
            "journal",
            "journal",
            "agent.log",
            "review.log",
        ]
        # continuation line folded into its event
        agent_first = events[0]
        assert agent_first["ts"] == LOG_TS_A
        assert "started dispatch" in agent_first["message"]
        assert "plain continuation line" in agent_first["message"]
        # journal events carry step as kind, summary (+hint) as message
        plan_ev = events[1]
        assert plan_ev["source"] == "journal"
        assert plan_ev["kind"] == "plan"
        assert plan_ev["message"] == "read the brief"
        impl_ev = events[2]
        assert impl_ev["kind"] == "implement"
        assert impl_ev["message"] == "wrote the parser; next: add tests"

    def test_sources_echo_all_true(self, client, plan_dir, worktree_dir):
        _write_manifest(plan_dir, "demo", {"S1": _dispatched_story(worktree_dir)})
        _write_journal(plan_dir, "demo", "S1", [{"step": "s", "ts": JOURNAL_TS_A}])
        _write_worktree(
            worktree_dir,
            "S1",
            {
                "agent.log": f"{LOG_TS_A} a\n",
                "review.log": f"{LOG_TS_A} r\n",
            },
        )
        body = _get(client).json()
        assert body["sources"] == {"journal": True, "agent.log": True, "review.log": True}

    def test_journal_only_story_is_available(self, client, plan_dir):
        _write_manifest(plan_dir, "demo", {"S1": _undispatched_story()})
        _write_journal(
            plan_dir, "demo", "S1", [{"step": "plan", "summary": "ok", "ts": JOURNAL_TS_A}]
        )
        res = _get(client)
        assert res.status_code == 200
        body = res.json()
        assert body["available"] is True
        assert [e["source"] for e in body["events"]] == ["journal"]
        assert body["sources"] == {
            "journal": True,
            "agent.log": False,
            "review.log": False,
        }

    def test_logs_only_story_is_available(self, client, plan_dir, worktree_dir):
        _write_manifest(plan_dir, "demo", {"S1": _dispatched_story(worktree_dir)})
        _write_worktree(worktree_dir, "S1", {"agent.log": f"{LOG_TS_A} a\n"})
        body = _get(client).json()
        assert body["available"] is True
        assert [e["source"] for e in body["events"]] == ["agent.log"]
        assert body["sources"]["journal"] is False
        assert body["sources"]["agent.log"] is True


# ---------------------------------------------------------------------------
# degraded states: 200 + available:false, never 500
# ---------------------------------------------------------------------------


class TestDegradedStates:
    def test_never_dispatched_story_no_journal(self, client, plan_dir):
        _write_manifest(plan_dir, "demo", {"S1": _undispatched_story()})
        res = _get(client)
        assert res.status_code == 200
        body = res.json()
        assert body["available"] is False
        assert body["events"] == []
        assert body["sources"] == {
            "journal": False,
            "agent.log": False,
            "review.log": False,
        }

    def test_corrupt_journal_and_missing_logs(self, client, plan_dir, worktree_dir):
        _write_manifest(plan_dir, "demo", {"S1": _dispatched_story(worktree_dir)})
        (plan_dir / "demo.S1.journal.json").write_text("{not json at all")
        # worktree exists but both logs are absent
        _write_worktree(worktree_dir, "S1", {})
        res = _get(client)
        assert res.status_code == 200
        body = res.json()
        assert body["available"] is False
        assert body["events"] == []

    def test_wiped_agent_log_only(self, client, plan_dir, worktree_dir):
        _write_manifest(plan_dir, "demo", {"S1": _dispatched_story(worktree_dir)})
        _write_worktree(worktree_dir, "S1", {"agent.log": ""})
        body = _get(client).json()
        assert body["available"] is False
        assert body["events"] == []

    def test_empty_journal_file(self, client, plan_dir):
        _write_manifest(plan_dir, "demo", {"S1": _undispatched_story()})
        (plan_dir / "demo.S1.journal.json").write_text("[]")
        body = _get(client).json()
        assert body["available"] is False
        assert body["events"] == []

    def test_missing_manifest_story_dict_entry(self, client, plan_dir):
        """A story whose manifest entry is not a dict must not 500."""
        _write_manifest(plan_dir, "demo", {"S1": "garbage"})
        res = _get(client)
        assert res.status_code == 200
        assert res.json()["available"] is False


# ---------------------------------------------------------------------------
# 404 contract (mirrors get_story_journal verbatim in shape)
# ---------------------------------------------------------------------------


class TestNotFound:
    def test_unknown_plan_404(self, client, plan_dir):
        res = _get(client, plan="nosuch")
        assert res.status_code == 404
        assert res.json()["detail"] == "No manifest for plan 'nosuch'"

    def test_unknown_story_in_existing_plan_404(self, client, plan_dir):
        _write_manifest(plan_dir, "demo", {"S1": _undispatched_story()})
        res = _get(client, story="S9")
        assert res.status_code == 404
        assert res.json()["detail"] == "No story 'S9' in plan 'demo'"

    def test_corrupt_manifest_is_404_not_500(self, client, plan_dir):
        (plan_dir / "demo.manifest.json").write_text("{broken")
        res = _get(client)
        assert res.status_code == 404
        assert res.json()["detail"] == "No manifest for plan 'demo'"


# ---------------------------------------------------------------------------
# lines query param: tail per log source, clamped to [1, 500]
# ---------------------------------------------------------------------------


class TestLinesParam:
    @staticmethod
    def _numbered_log(n: int) -> str:
        # Timestamped lines so each becomes its own replay event.
        return "".join(
            f"2025-01-01T10:{m:02d}:00Z line {i}\n"
            for i, (h, m) in enumerate(
                [(i // 60) % 24, i % 60] for i in range(n)
            )
        )

    def test_lines_5_returns_at_most_5_tail_lines_per_source(
        self, client, plan_dir, worktree_dir
    ):
        _write_manifest(plan_dir, "demo", {"S1": _dispatched_story(worktree_dir)})
        _write_worktree(
            worktree_dir,
            "S1",
            {
                "agent.log": self._numbered_log(20),
                "review.log": self._numbered_log(20),
            },
        )
        body = _get(client, query="?lines=5").json()
        agent = [e for e in body["events"] if e["source"] == "agent.log"]
        review = [e for e in body["events"] if e["source"] == "review.log"]
        assert len(agent) == 5
        assert len(review) == 5
        # tail, not head: the LAST 5 lines win
        assert "line 19" in agent[-1]["message"]
        assert "line 15" in agent[0]["message"]
        assert not any("line 14" in e["message"] for e in agent)

    def test_lines_default_200(self, client, plan_dir, worktree_dir):
        _write_manifest(plan_dir, "demo", {"S1": _dispatched_story(worktree_dir)})
        _write_worktree(worktree_dir, "S1", {"agent.log": self._numbered_log(300)})
        body = _get(client).json()
        agent = [e for e in body["events"] if e["source"] == "agent.log"]
        assert len(agent) == 200
        assert "line 299" in agent[-1]["message"]

    def test_lines_zero_clamps_to_one(self, client, plan_dir, worktree_dir):
        _write_manifest(plan_dir, "demo", {"S1": _dispatched_story(worktree_dir)})
        _write_worktree(worktree_dir, "S1", {"agent.log": self._numbered_log(10)})
        body = _get(client, query="?lines=0").json()
        agent = [e for e in body["events"] if e["source"] == "agent.log"]
        assert len(agent) == 1
        assert "line 9" in agent[0]["message"]

    def test_lines_negative_clamps_to_one(self, client, plan_dir, worktree_dir):
        _write_manifest(plan_dir, "demo", {"S1": _dispatched_story(worktree_dir)})
        _write_worktree(worktree_dir, "S1", {"agent.log": self._numbered_log(10)})
        body = _get(client, query="?lines=-7").json()
        agent = [e for e in body["events"] if e["source"] == "agent.log"]
        assert len(agent) == 1

    def test_lines_9999_caps_at_500(self, client, plan_dir, worktree_dir):
        _write_manifest(plan_dir, "demo", {"S1": _dispatched_story(worktree_dir)})
        _write_worktree(worktree_dir, "S1", {"agent.log": self._numbered_log(600)})
        body = _get(client, query="?lines=9999").json()
        agent = [e for e in body["events"] if e["source"] == "agent.log"]
        assert len(agent) == 500
        assert "line 599" in agent[-1]["message"]

    def test_lines_boundary_500_exact(self, client, plan_dir, worktree_dir):
        _write_manifest(plan_dir, "demo", {"S1": _dispatched_story(worktree_dir)})
        _write_worktree(worktree_dir, "S1", {"agent.log": self._numbered_log(501)})
        body = _get(client, query="?lines=500").json()
        agent = [e for e in body["events"] if e["source"] == "agent.log"]
        assert len(agent) == 500

    def test_lines_boundary_1_exact(self, client, plan_dir, worktree_dir):
        _write_manifest(plan_dir, "demo", {"S1": _dispatched_story(worktree_dir)})
        _write_worktree(worktree_dir, "S1", {"agent.log": self._numbered_log(3)})
        body = _get(client, query="?lines=1").json()
        agent = [e for e in body["events"] if e["source"] == "agent.log"]
        assert len(agent) == 1
        assert "line 2" in agent[0]["message"]

    def test_lines_clamp_applies_per_source_independently(
        self, client, plan_dir, worktree_dir
    ):
        _write_manifest(plan_dir, "demo", {"S1": _dispatched_story(worktree_dir)})
        _write_worktree(
            worktree_dir,
            "S1",
            {"agent.log": self._numbered_log(3), "review.log": self._numbered_log(9)},
        )
        body = _get(client, query="?lines=5").json()
        assert len([e for e in body["events"] if e["source"] == "agent.log"]) == 3
        assert len([e for e in body["events"] if e["source"] == "review.log"]) == 5


# ---------------------------------------------------------------------------
# path traversal: an escaping worktree contributes nothing, still 200
# ---------------------------------------------------------------------------


class TestWorktreeContainment:
    def test_escaping_worktree_contributes_nothing(self, client, plan_dir, worktree_dir):
        _write_manifest(
            plan_dir,
            "demo",
            {
                "S1": {
                    "summary": "hand-edited",
                    "status": "in_progress",
                    "worktree": str(worktree_dir.parent / "elsewhere"),
                    "dependencies": [],
                },
            },
        )
        # The escaped location exists and even contains an agent.log, but it
        # is outside WORKTREE_ROOT so the store must refuse it.
        escaped = worktree_dir.parent / "elsewhere"
        escaped.mkdir()
        (escaped / "agent.log").write_text(f"{LOG_TS_A} secret\n")

        res = _get(client)
        assert res.status_code == 200
        body = res.json()
        assert body["available"] is False
        assert body["events"] == []
        assert body["sources"]["agent.log"] is False

    def test_relative_worktree_path_contributes_nothing(
        self, client, plan_dir, worktree_dir
    ):
        _write_manifest(
            plan_dir,
            "demo",
            {
                "S1": {
                    "summary": "relative",
                    "status": "in_progress",
                    "worktree": "relative/wt",
                    "dependencies": [],
                },
            },
        )
        res = _get(client)
        assert res.status_code == 200
        assert res.json()["available"] is False