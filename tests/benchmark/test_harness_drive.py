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

import harness  # noqa: E402


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

    def advance_pipeline(self, plan_name: str) -> dict:
        i = self.calls
        self.calls += 1
        manifest_path = self.PLAN_DIR / f"{plan_name}.manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["stories"][self.story_key]["status"] = self.statuses[i]
        manifest_path.write_text(json.dumps(manifest))
        return self.summaries[i]


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
