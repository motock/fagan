"""retros/PENDING.md completion markers must use the right noun for the count.

The marker written when a plan finishes read ``completed <date>, 1 stories``
for a one-story plan. It must read ``1 story`` and keep ``N stories`` for every
other count (including 0). The marker's dedup-by-plan-name behavior is
unchanged.

Assertions read the throwaway PENDING.md these tests point the writer at;
they never touch the real, shared retros/PENDING.md.
"""

import importlib
import re

import pytest

p = importlib.import_module("pipeline.server")
ci = importlib.import_module("pipeline.ci")
merge = importlib.import_module("pipeline.merge")

_DATE = r"\d{4}-\d{2}-\d{2}"


def _expected_marker(plan, count, noun):
    prefix = re.escape(f"- {plan} — completed ")
    return re.compile(prefix + _DATE + re.escape(f", {count} {noun}"))


@pytest.mark.parametrize(
    ("count", "noun"),
    [(0, "stories"), (1, "story"), (2, "stories"), (11, "stories")],
)
def test_marker_uses_the_right_noun_for_the_story_count(tmp_path, monkeypatch, count, noun):
    pending = tmp_path / "PENDING.md"
    monkeypatch.setattr(p, "RETRO_PENDING_PATH", pending, raising=False)

    ci._record_retro_pending("grammar-plan", count)

    assert _expected_marker("grammar-plan", count, noun).fullmatch(pending.read_text().strip())


def test_a_one_story_marker_never_says_1_stories(tmp_path, monkeypatch):
    pending = tmp_path / "PENDING.md"
    monkeypatch.setattr(p, "RETRO_PENDING_PATH", pending, raising=False)

    ci._record_retro_pending("grammar-plan", 1)

    assert "1 stories" not in pending.read_text()


def test_the_real_merge_path_writes_the_singular_marker(tmp_path, monkeypatch):
    pending = tmp_path / "PENDING.md"
    monkeypatch.setattr(p, "RETRO_PENDING_PATH", pending, raising=False)
    manifest = {
        "repo_root": str(p.PIPELINE_SELF_REPO_ROOT),
        "stories": {"a": {"status": "done"}},
    }

    merge._maybe_record_retro("one-story-plan", manifest)

    assert _expected_marker("one-story-plan", 1, "story").fullmatch(pending.read_text().strip())


def test_a_second_call_for_the_same_plan_appends_nothing(tmp_path, monkeypatch):
    pending = tmp_path / "PENDING.md"
    monkeypatch.setattr(p, "RETRO_PENDING_PATH", pending, raising=False)

    ci._record_retro_pending("grammar-plan", 1)
    ci._record_retro_pending("grammar-plan", 1)

    assert len(pending.read_text().splitlines()) == 1
