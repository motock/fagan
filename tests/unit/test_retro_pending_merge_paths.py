"""RETRO-01: both merge paths must feed the retro PENDING backlog.

mark_story_done already appends to retros/PENDING.md when a fully-done self-repo
plan completes (pipeline/ci.py::_mark_story_done_impl). approve_merge
(_approve_merge_impl) and the advance-scheduler merge branch set
``story["status"] = "done"`` without the same check — a fully-done self-repo
plan only enters the retro backlog if its LAST story happens to go through
mark_story_done. These tests pin the new wiring in both remaining paths.

Assertions are membership-style only — retros/PENDING.md is a shared, cumulative
artifact (other plans append to it), so never assert its full contents.
"""

import importlib
import inspect

p = importlib.import_module("pipeline.server")


def test_maybe_record_retro_appends_when_all_done_and_self_repo(tmp_path, monkeypatch):
    pending = tmp_path / "PENDING.md"
    monkeypatch.setattr(p, "RETRO_PENDING_PATH", pending, raising=False)
    merge = importlib.import_module("pipeline.merge")

    manifest = {
        "repo_root": str(p.PIPELINE_SELF_REPO_ROOT),
        "stories": {"a": {"status": "done"}, "b": {"status": "done"}},
    }
    merge._maybe_record_retro("retro-merge-append-hook-test", manifest)

    lines = pending.read_text().splitlines() if pending.exists() else []
    hits = [ln for ln in lines if ln.startswith("- retro-merge-append-hook-test ")]
    assert len(hits) == 1


def test_maybe_record_retro_skips_when_not_all_done(tmp_path, monkeypatch):
    pending = tmp_path / "PENDING.md"
    monkeypatch.setattr(p, "RETRO_PENDING_PATH", pending, raising=False)
    merge = importlib.import_module("pipeline.merge")

    manifest = {
        "repo_root": str(p.PIPELINE_SELF_REPO_ROOT),
        "stories": {"a": {"status": "done"}, "b": {"status": "tests_passed"}},
    }
    merge._maybe_record_retro("retro-partial-not-done", manifest)

    assert not pending.exists()


def test_maybe_record_retro_skips_when_repo_root_is_not_self(tmp_path, monkeypatch):
    pending = tmp_path / "PENDING.md"
    monkeypatch.setattr(p, "RETRO_PENDING_PATH", pending, raising=False)
    merge = importlib.import_module("pipeline.merge")

    manifest = {
        "repo_root": "/tmp/some-other-repo",
        "stories": {"a": {"status": "done"}},
    }
    merge._maybe_record_retro("retro-external-plan", manifest)

    assert not pending.exists()


def test_maybe_record_retro_skips_when_no_stories(tmp_path, monkeypatch):
    pending = tmp_path / "PENDING.md"
    monkeypatch.setattr(p, "RETRO_PENDING_PATH", pending, raising=False)
    merge = importlib.import_module("pipeline.merge")

    manifest = {"repo_root": str(p.PIPELINE_SELF_REPO_ROOT)}
    merge._maybe_record_retro("retro-empty-plan", manifest)

    assert not pending.exists()


def test_advance_serverref_resolves_to_merge_helper(tmp_path, monkeypatch):
    advance = importlib.import_module("pipeline.advance")
    merge = importlib.import_module("pipeline.merge")
    assert advance._maybe_record_retro._value() is p._maybe_record_retro
    assert advance._maybe_record_retro._value() is merge._maybe_record_retro

    # Dedup: a pre-seeded PENDING.md entry must not be appended a second time.
    pending = tmp_path / "PENDING.md"
    monkeypatch.setattr(p, "RETRO_PENDING_PATH", pending, raising=False)
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text("- dedup-plan — completed earlier, 1 stories\n")
    before = pending.read_text()

    advance._maybe_record_retro(
        "dedup-plan",
        {"repo_root": str(p.PIPELINE_SELF_REPO_ROOT), "stories": {"a": {"status": "done"}}},
    )
    hits = [ln for ln in pending.read_text().splitlines() if ln.startswith("- dedup-plan ")]
    assert len(hits) == 1
    assert pending.read_text() == before


def test_approve_merge_impl_calls_maybe_record_retro():
    import pipeline.merge

    # Structural wiring assertion: a full _approve_merge_impl call needs the
    # plan lock + CI/PR mocks; the unit tests above cover _maybe_record_retro's
    # behavior, so here we pin the call-site wiring via source inspection.
    src = inspect.getsource(pipeline.merge._approve_merge_impl)
    assert "_maybe_record_retro(plan_name, manifest)" in src


def test_advance_pipeline_locked_impl_calls_maybe_record_retro():
    import pipeline.advance

    # Mirror of the merge.py structural pin above: the advance-scheduler merge
    # branch must also feed the backlog after marking a story done. A full
    # tick run needs the plan lock + a merged PR, so the call site is pinned
    # via source inspection. LOCKSTARVE-A1 extracted the merge-adjudication
    # block (including this call) out of _advance_pipeline_locked_impl into
    # the module-level _adjudicate_merges helper it now calls, so the pin
    # follows the call site to its new home.
    src = inspect.getsource(pipeline.advance._adjudicate_merges)
    assert "_maybe_record_retro(plan_name, manifest)" in src
