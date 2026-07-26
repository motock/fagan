"""Unit tests for harness.drive()'s deadline handling around reviewer
rate-limit deferral (FM-H).

advance_pipeline reports a story as "review_deferred" when the reviewer hit an
infra rate-limit rather than giving a genuine verdict (see review_story /
_is_rate_limited in pipeline_mcp_server.py). Ticking the wall-clock deadline
loop through a long rate-limit window would otherwise burn the whole benchmark
budget waiting on Claude's reset rather than on real work, so drive() extends
its deadline while (and only while) a story is stuck on a deferred review --
bounded by max_defer_extension so a permanently-stuck condition still times
out eventually.

Uses a fake `p` (no real pipeline_mcp_server call) and a fake wall clock (no
real sleeping), so these run in milliseconds and don't need a model/network.
"""
import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

import harness


class _FakeClock:
    """Drop-in replacement for the stdlib `time` module used by drive():
    time.sleep(s) advances the fake clock by s instead of actually blocking."""

    def __init__(self, start: float = 0.0):
        self.now = start

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _FakePipeline:
    """Fake advance_pipeline() that plays back a canned sequence of summaries
    and, as a side effect, writes the matching story status to the on-disk
    manifest -- mirroring what the real advance_pipeline does."""

    def __init__(self, plan_dir: Path, story_key: str, statuses, summaries):
        self.PLAN_DIR = plan_dir
        self.story_key = story_key
        self.statuses = list(statuses)
        self.summaries = list(summaries)
        self.calls = 0
        self.interrupted: list[str] = []

    def advance_pipeline(self, plan_name: str) -> dict:
        i = self.calls
        self.calls += 1
        manifest_path = self.PLAN_DIR / f"{plan_name}.manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["stories"][self.story_key]["status"] = self.statuses[i]
        manifest_path.write_text(json.dumps(manifest))
        return self.summaries[i]

    def interrupt_story(self, plan_name: str, story_key: str) -> dict:
        self.interrupted.append(story_key)
        return {"ok": True, "status": "interrupted"}


def _write_manifest(plan_dir: Path, plan_name: str, story_key: str, status: str) -> None:
    manifest_path = plan_dir / f"{plan_name}.manifest.json"
    manifest_path.write_text(json.dumps({"stories": {story_key: {"status": status}}}))


def test_drive_extends_deadline_while_review_is_rate_limited(tmp_path, monkeypatch):
    plan_dir = tmp_path
    plan_name = "planx"
    story_key = "S1"
    _write_manifest(plan_dir, plan_name, story_key, "tests_passed")

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr(harness, "time", clock)

    # 5 ticks deferred (still tests_passed), 6th tick reaches a terminal status.
    statuses = ["tests_passed"] * 5 + ["done"]
    summaries = [{"review_deferred": [story_key]} for _ in range(5)] + [
        {"review_deferred": []}
    ]
    fake_p = _FakePipeline(plan_dir, story_key, statuses, summaries)

    # Deadline only covers ~3 ticks unextended: without extension, the loop
    # would time out at "tests_passed" long before the 6th (terminal) tick.
    deadline = clock.now + 3.0
    ticks = harness.drive(fake_p, plan_name, story_key, deadline, tick_interval=1.0,
                          max_defer_extension=100.0)

    assert fake_p.calls == 6
    assert ticks[-1]["status"] == "done"


def test_drive_stops_extending_once_max_defer_extension_is_exhausted(tmp_path, monkeypatch):
    plan_dir = tmp_path
    plan_name = "plany"
    story_key = "S1"
    _write_manifest(plan_dir, plan_name, story_key, "tests_passed")

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr(harness, "time", clock)

    # The story never leaves tests_passed and review is deferred every tick --
    # simulating a reviewer rate-limit that never clears within the run.
    statuses = ["tests_passed"] * 10
    summaries = [{"review_deferred": [story_key]} for _ in range(10)]
    fake_p = _FakePipeline(plan_dir, story_key, statuses, summaries)

    deadline = clock.now + 1.0
    ticks = harness.drive(fake_p, plan_name, story_key, deadline, tick_interval=1.0,
                          max_defer_extension=2.0)

    # Extension is capped at 2.0 (two 1.0s extensions), so the loop must stop
    # once the (twice-extended) deadline is finally reached, with the story
    # still stuck at a non-terminal status.
    assert ticks[-1]["status"] == "tests_passed"
    assert ticks[-1]["status"] not in harness.TERMINAL
    assert fake_p.calls == 3


class _FakeMultiPipeline:
    """Like _FakePipeline, but plays back statuses for several story keys at
    once -- for drive_plan(), which waits on a dependency-chained plan rather
    than a single story."""

    def __init__(self, plan_dir: Path, story_keys, statuses_by_key, summaries):
        self.PLAN_DIR = plan_dir
        self.story_keys = story_keys
        self.statuses_by_key = statuses_by_key
        self.summaries = list(summaries)
        self.calls = 0
        self.interrupted: list[str] = []

    def advance_pipeline(self, plan_name: str) -> dict:
        i = self.calls
        self.calls += 1
        manifest_path = self.PLAN_DIR / f"{plan_name}.manifest.json"
        manifest = json.loads(manifest_path.read_text())
        for key in self.story_keys:
            manifest["stories"][key]["status"] = self.statuses_by_key[key][i]
        manifest_path.write_text(json.dumps(manifest))
        return self.summaries[i]

    def interrupt_story(self, plan_name: str, story_key: str) -> dict:
        self.interrupted.append(story_key)
        return {"ok": True, "status": "interrupted"}


def _write_multi_manifest(plan_dir: Path, plan_name: str, story_keys, initial_status: str) -> None:
    manifest_path = plan_dir / f"{plan_name}.manifest.json"
    manifest_path.write_text(json.dumps(
        {"stories": {key: {"status": initial_status} for key in story_keys}}
    ))


def test_drive_plan_waits_for_every_story_key_to_reach_terminal(tmp_path, monkeypatch):
    plan_dir = tmp_path
    plan_name = "plan_multi"
    story_keys = ["S1", "S2"]
    _write_multi_manifest(plan_dir, plan_name, story_keys, "dispatched")

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr(harness, "time", clock)

    # S1 (the dependency) finishes on tick 2; S2 (depends on S1) only finishes
    # on tick 4 -- drive_plan must not stop early just because S1 is terminal.
    statuses_by_key = {
        "S1": ["dispatched", "done", "done", "done", "done"],
        "S2": ["dispatched", "dispatched", "dispatched", "dispatched", "done"],
    }
    summaries = [{"review_deferred": []} for _ in range(5)]
    fake_p = _FakeMultiPipeline(plan_dir, story_keys, statuses_by_key, summaries)

    deadline = clock.now + 10.0
    ticks = harness.drive_plan(fake_p, plan_name, story_keys, deadline, tick_interval=1.0)

    assert fake_p.calls == 5
    assert ticks[-1]["statuses"] == {"S1": "done", "S2": "done"}


def test_drive_plan_extends_deadline_while_any_story_is_review_deferred(tmp_path, monkeypatch):
    plan_dir = tmp_path
    plan_name = "plan_multi_defer"
    story_keys = ["S1", "S2"]
    _write_multi_manifest(plan_dir, plan_name, story_keys, "tests_passed")

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr(harness, "time", clock)

    # S1 is already done; S2 is stuck under reviewer rate-limit (FM-H) for 5
    # ticks before finally landing on the 6th.
    statuses_by_key = {
        "S1": ["done"] * 6,
        "S2": ["tests_passed"] * 5 + ["done"],
    }
    summaries = [{"review_deferred": ["S2"]} for _ in range(5)] + [{"review_deferred": []}]
    fake_p = _FakeMultiPipeline(plan_dir, story_keys, statuses_by_key, summaries)

    # Deadline only covers ~3 ticks unextended.
    deadline = clock.now + 3.0
    ticks = harness.drive_plan(fake_p, plan_name, story_keys, deadline, tick_interval=1.0,
                               max_defer_extension=100.0)

    assert fake_p.calls == 6
    assert ticks[-1]["statuses"] == {"S1": "done", "S2": "done"}


def test_drive_plan_stops_early_when_a_dependency_permanently_fails(tmp_path, monkeypatch):
    """S2 depends on S1; list_ready_stories only dispatches a story once every
    dependency is "done" (see pipeline_mcp_server.py), so once S1 lands on
    "failed" (a TERMINAL status, but not "done"), S2 can never become ready --
    it would sit at "todo" forever. drive_plan must recognize the whole plan
    is stuck and stop, instead of polling until the wall-clock deadline."""
    plan_dir = tmp_path
    plan_name = "plan_stuck"
    story_keys = ["S1", "S2"]
    manifest_path = plan_dir / f"{plan_name}.manifest.json"
    manifest_path.write_text(json.dumps({"stories": {
        "S1": {"status": "dispatched", "dependencies": []},
        "S2": {"status": "todo", "dependencies": ["S1"]},
    }}))

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr(harness, "time", clock)

    # S1 fails on tick 2 and stays failed; S2 never leaves "todo" because its
    # only dependency never reaches "done".
    statuses_by_key = {
        "S1": ["dispatched", "failed", "failed", "failed", "failed"],
        "S2": ["todo", "todo", "todo", "todo", "todo"],
    }
    summaries = [{"review_deferred": []} for _ in range(5)]
    fake_p = _FakeMultiPipeline(plan_dir, story_keys, statuses_by_key, summaries)

    # A deadline generous enough that, without stuck-detection, the loop would
    # keep polling well past when S1 actually failed.
    deadline = clock.now + 100.0
    ticks = harness.drive_plan(fake_p, plan_name, story_keys, deadline, tick_interval=1.0)

    assert ticks[-1]["statuses"] == {"S1": "failed", "S2": "todo"}
    # Stopped as soon as S1's failure made S2 permanently unreachable (tick 2),
    # not after burning the full 100-tick deadline.
    assert fake_p.calls == 2


def test_drive_plan_stops_early_for_a_transitively_blocked_dependency_chain(tmp_path, monkeypatch):
    """S3 depends on S2, which depends on S1. S1 parks; S2 never leaves "todo"
    (it's blocked directly), and S3 never leaves "todo" either -- but S3's own
    DIRECT dependency (S2) never reaches a terminal status itself, so
    detecting S3 as blocked requires following the chain through S2 to S1,
    not just checking S3's immediate dependency's status string."""
    plan_dir = tmp_path
    plan_name = "plan_stuck_chain"
    story_keys = ["S1", "S2", "S3"]
    manifest_path = plan_dir / f"{plan_name}.manifest.json"
    manifest_path.write_text(json.dumps({"stories": {
        "S1": {"status": "dispatched", "dependencies": []},
        "S2": {"status": "todo", "dependencies": ["S1"]},
        "S3": {"status": "todo", "dependencies": ["S2"]},
    }}))

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr(harness, "time", clock)

    statuses_by_key = {
        "S1": ["dispatched", "parked", "parked", "parked", "parked"],
        "S2": ["todo", "todo", "todo", "todo", "todo"],
        "S3": ["todo", "todo", "todo", "todo", "todo"],
    }
    summaries = [{"review_deferred": []} for _ in range(5)]
    fake_p = _FakeMultiPipeline(plan_dir, story_keys, statuses_by_key, summaries)

    deadline = clock.now + 100.0
    ticks = harness.drive_plan(fake_p, plan_name, story_keys, deadline, tick_interval=1.0)

    assert ticks[-1]["statuses"] == {"S1": "parked", "S2": "todo", "S3": "todo"}
    assert fake_p.calls == 2


def test_drive_reaps_dispatch_when_deadline_passes_without_terminal_status(
    tmp_path, monkeypatch,
):
    """A dispatch subprocess that's still running when drive()'s own deadline
    passes must not be left orphaned (observed directly: an MLX dispatch
    survived past the harness's own timeout, found still running minutes
    later, had to be killed manually). drive() must call interrupt_story on
    the still-outstanding story before giving up."""
    plan_dir = tmp_path
    plan_name = "plan_hung"
    story_key = "S1"
    _write_manifest(plan_dir, plan_name, story_key, "in_progress")

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr(harness, "time", clock)

    # The story never leaves "in_progress" — simulating a hung dispatch
    # subprocess that produced no output and never reached a terminal status.
    statuses = ["in_progress"] * 5
    summaries = [{"review_deferred": []} for _ in range(5)]
    fake_p = _FakePipeline(plan_dir, story_key, statuses, summaries)

    deadline = clock.now + 3.0
    ticks = harness.drive(fake_p, plan_name, story_key, deadline, tick_interval=1.0)

    assert ticks[-1]["status"] == "in_progress"
    assert fake_p.interrupted == [story_key]


def test_drive_does_not_reap_when_story_reaches_terminal_status(tmp_path, monkeypatch):
    """The common case: the story finishes within the deadline. No reap call
    should ever fire — the dispatch subprocess already exited on its own."""
    plan_dir = tmp_path
    plan_name = "plan_ok"
    story_key = "S1"
    _write_manifest(plan_dir, plan_name, story_key, "dispatched")

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr(harness, "time", clock)

    statuses = ["dispatched", "tests_passed", "done"]
    summaries = [{"review_deferred": []} for _ in range(3)]
    fake_p = _FakePipeline(plan_dir, story_key, statuses, summaries)

    deadline = clock.now + 10.0
    ticks = harness.drive(fake_p, plan_name, story_key, deadline, tick_interval=1.0)

    assert ticks[-1]["status"] == "done"
    assert fake_p.interrupted == []


def test_drive_plan_reaps_non_terminal_stories_when_deadline_passes(tmp_path, monkeypatch):
    """Same reasoning as drive()'s single-story case, for a dependency-chained
    plan: any story still non-terminal when drive_plan gives up must be
    reaped rather than left as an orphaned subprocess."""
    plan_dir = tmp_path
    plan_name = "plan_multi_hung"
    story_keys = ["S1", "S2"]
    manifest_path = plan_dir / f"{plan_name}.manifest.json"
    manifest_path.write_text(json.dumps({"stories": {
        "S1": {"status": "dispatched", "dependencies": [], "pid": 111},
        "S2": {"status": "dispatched", "dependencies": [], "pid": 222},
    }}))

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr(harness, "time", clock)

    # S1 finishes; S2 hangs indefinitely in "in_progress".
    statuses_by_key = {
        "S1": ["dispatched", "done", "done", "done"],
        "S2": ["dispatched", "in_progress", "in_progress", "in_progress"],
    }
    summaries = [{"review_deferred": []} for _ in range(4)]
    fake_p = _FakeMultiPipeline(plan_dir, story_keys, statuses_by_key, summaries)

    deadline = clock.now + 3.0
    ticks = harness.drive_plan(fake_p, plan_name, story_keys, deadline, tick_interval=1.0)

    assert ticks[-1]["statuses"]["S2"] == "in_progress"
    assert fake_p.interrupted == ["S2"]


def test_drive_plan_does_not_reap_a_never_dispatched_blocked_story(tmp_path, monkeypatch):
    """S2 depends on S1, which fails — S2 is permanently blocked and stays
    "todo" (never dispatched, no subprocess to reap). Only S1, which actually
    ran, is a candidate for reaping — but S1 already reached its own terminal
    status ("failed"), so nothing should be reaped at all here."""
    plan_dir = tmp_path
    plan_name = "plan_blocked_no_reap"
    story_keys = ["S1", "S2"]
    manifest_path = plan_dir / f"{plan_name}.manifest.json"
    manifest_path.write_text(json.dumps({"stories": {
        "S1": {"status": "dispatched", "dependencies": [], "pid": 111},
        "S2": {"status": "todo", "dependencies": ["S1"]},
    }}))

    clock = _FakeClock(start=0.0)
    monkeypatch.setattr(harness, "time", clock)

    statuses_by_key = {
        "S1": ["dispatched", "failed", "failed"],
        "S2": ["todo", "todo", "todo"],
    }
    summaries = [{"review_deferred": []} for _ in range(3)]
    fake_p = _FakeMultiPipeline(plan_dir, story_keys, statuses_by_key, summaries)

    deadline = clock.now + 100.0
    ticks = harness.drive_plan(fake_p, plan_name, story_keys, deadline, tick_interval=1.0)

    assert ticks[-1]["statuses"] == {"S1": "failed", "S2": "todo"}
    assert fake_p.interrupted == []
