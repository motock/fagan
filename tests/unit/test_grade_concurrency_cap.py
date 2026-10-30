"""SA-19: cap concurrent detached grades and optionally their xdist workers.

The process boundary is monkeypatched: ``_is_pid_alive`` stands in for the
``os.kill(pid, 0)`` + ``ps -o stat=`` probe, so no real process is spawned or
signalled here. The manifest directory is a real temp dir on disk so the
glob/parse behaviour (and "no manifest write on a blocked tick") is exercised
for real.
"""

import json

import pytest

from pipeline import detached_grade


def _write_json(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_tick(tmp_path, monkeypatch, *, live_pids, env=None, others=None,
               story_key="mine", test_cmd=None):
    """Build one scheduler-tick invocation of the real lifecycle.

    Returns (story, manifest_path, calls, run_tick); ``run_tick()`` performs a
    single tick and returns the lifecycle's (early_result, test_result) tuple.
    ``others`` maps a manifest filename to either a dict payload or raw text.
    """
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"{story_key}.manifest.json"
    story = {"status": "in_progress", "pid": 1234}
    manifest = {"stories": {story_key: story}}
    _write_json(manifest_path, manifest)
    for name, payload in (others or {}).items():
        target = manifest_dir / name
        if isinstance(payload, str):
            target.write_text(payload, encoding="utf-8")
        else:
            _write_json(target, payload)

    calls = []

    def starter(cmd, cwd, run_env, result_path, log_path):
        calls.append(
            {
                "cmd": cmd,
                "cwd": cwd,
                "env": run_env,
                "result_path": result_path,
                "log_path": log_path,
            }
        )
        return 9001

    monkeypatch.setattr(detached_grade, "start_detached_grade", starter)
    monkeypatch.setattr(
        detached_grade,
        "_is_pid_alive",
        lambda pid: pid in live_pids,
        raising=False,
    )
    # The untrack helper shells out to git; this lifecycle test is not about
    # it, so keep the tick from doing any git work.
    monkeypatch.setattr(detached_grade, "_untrack_scratchpad", None, raising=False)

    for var in (
        "PIPELINE_MAX_CONCURRENT_GRADES",
        "PIPELINE_GRADE_XDIST_WORKERS",
        "PIPELINE_STATE_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    for var, value in (env or {}).items():
        monkeypatch.setenv(var, value)

    cmd = test_cmd or [".venv/bin/python", "-m", "pytest", "-q"]
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)

    def run_tick():
        return detached_grade._detached_grade_lifecycle(
            story,
            story_key,
            story["pid"],
            worktree,
            manifest,
            manifest_path,
            cmd,
            tmp_path,
            {"PATH": "/usr/bin"},
        )

    return story, manifest_path, calls, run_tick


# --------------------------------------------------------------------------
# Step 3 - the cap guard, driven through the real lifecycle.
# --------------------------------------------------------------------------


def test_cap_one_with_a_live_grade_queues_instead_of_starting(tmp_path, monkeypatch):
    """Cap 1 + one other story's live grading_pid -> no spawn, story untouched.

    Tick 1: live_grade_count == 1, 1 >= 1 -> starter NOT called; the story
    keeps status in_progress with no grading fields and its manifest bytes are
    unchanged. Then tick 2 in the same process, with the other pid now dead:
    the cap no longer blocks, so the starter runs exactly once in total and
    only then do the grading fields appear.
    """
    live = {4242}
    story, manifest_path, calls, run_tick = _make_tick(
        tmp_path,
        monkeypatch,
        live_pids=live,
        others={"other.manifest.json": {"grading_pid": 4242}},
    )
    before = manifest_path.read_bytes()

    early, test_result = run_tick()

    assert calls == [], "starter was called once despite the cap"
    assert test_result is None
    assert early == {"status": "grading_queued", "pid": 1234}
    assert "grading_pid" not in story
    assert "grading_started_at" not in story
    assert "grading_result_path" not in story
    assert story["status"] == "in_progress"
    assert manifest_path.read_bytes() == before, "blocked tick rewrote the manifest"
    assert not (tmp_path / "manifests" / "grading").exists()

    # Tick 2: 4242 has exited (probe returns False), so the cap lifts.
    live.clear()
    early, test_result = run_tick()

    assert len(calls) == 1, "starter was not called once after the cap lifted"
    assert early == {"status": "grading", "pid": 9001}
    assert test_result is None
    assert story["grading_pid"] == 9001


def test_cap_one_with_a_dead_grade_starts_the_grade(tmp_path, monkeypatch):
    """Cap 1 + the other story's pid is dead -> starter called exactly once."""
    story, _manifest_path, calls, run_tick = _make_tick(
        tmp_path,
        monkeypatch,
        live_pids=set(),
        others={"other.manifest.json": {"grading_pid": 5555}},
    )

    early, test_result = run_tick()

    assert len(calls) == 1
    assert early == {"status": "grading", "pid": 9001}
    assert test_result is None
    assert story["grading_pid"] == 9001


def test_cap_two_allows_one_live_grade(tmp_path, monkeypatch):
    """Cap 2 with one live grade -> the starter still runs."""
    story, _manifest_path, calls, run_tick = _make_tick(
        tmp_path,
        monkeypatch,
        live_pids={4242},
        env={"PIPELINE_MAX_CONCURRENT_GRADES": "2"},
        others={"other.manifest.json": {"grading_pid": 4242}},
    )

    early, _test_result = run_tick()

    assert len(calls) == 1
    assert early == {"status": "grading", "pid": 9001}
    assert story["grading_pid"] == 9001


def test_malformed_manifest_is_skipped_and_does_not_raise(tmp_path, monkeypatch):
    """A broken manifest in the directory neither raises nor counts."""
    _story, _manifest_path, calls, run_tick = _make_tick(
        tmp_path,
        monkeypatch,
        live_pids={4242},
        others={
            "broken.manifest.json": "{not json",
            "other.manifest.json": {"grading_pid": 4242},
        },
    )

    early, _test_result = run_tick()  # must not raise

    assert early == {"status": "grading_queued", "pid": 1234}
    assert calls == []


# --------------------------------------------------------------------------
# Step 4 - env parsing (cap + optional xdist workers).
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_warn_cache(monkeypatch):
    """Give every test its own once-per-value WARN cache."""
    monkeypatch.setattr(
        detached_grade, "_WARNED_INVALID_VALUES", set(), raising=False
    )


@pytest.mark.parametrize("raw", ["0", "-1", "abc"])
def test_invalid_max_concurrent_grades_falls_back_to_one(raw, caplog):
    with caplog.at_level("WARNING"):
        assert detached_grade.max_concurrent_grades(
            environ={"PIPELINE_MAX_CONCURRENT_GRADES": raw}
        ) == 1
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "PIPELINE_MAX_CONCURRENT_GRADES" in message
    assert raw in message


def test_default_max_concurrent_grades_is_one(caplog):
    with caplog.at_level("WARNING"):
        assert detached_grade.max_concurrent_grades(environ={}) == 1
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


def test_invalid_cap_warns_once_per_distinct_value(caplog):
    """Cumulative trace: 0, 0, -1, abc, "", 3, 0 -> four warns, cap 3 on "3"."""
    with caplog.at_level("WARNING"):
        results = [
            detached_grade.max_concurrent_grades(
                environ={"PIPELINE_MAX_CONCURRENT_GRADES": raw}
            )
            for raw in ("0", "0", "-1", "abc", "", "3", "0")
        ]
    assert results == [1, 1, 1, 1, 1, 3, 1]
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 4


def test_xdist_workers_appended_to_pytest_command(tmp_path, monkeypatch):
    _story, _manifest_path, calls, run_tick = _make_tick(
        tmp_path,
        monkeypatch,
        live_pids=set(),
        env={"PIPELINE_GRADE_XDIST_WORKERS": "3"},
        test_cmd=[".venv/bin/python", "-m", "pytest", "-q"],
    )

    run_tick()

    assert len(calls) == 1
    assert calls[0]["cmd"] == [
        ".venv/bin/python",
        "-m",
        "pytest",
        "-q",
        "-n",
        "3",
    ]


def test_xdist_workers_unset_leaves_command_unchanged_and_does_not_mutate(
    tmp_path, monkeypatch
):
    """Two ticks with the SAME test_cmd list: the append must not leak."""
    shared = [".venv/bin/python", "-m", "pytest", "-q"]
    story, _manifest_path, calls, run_tick = _make_tick(
        tmp_path,
        monkeypatch,
        live_pids=set(),
        env={"PIPELINE_GRADE_XDIST_WORKERS": "3"},
        test_cmd=shared,
    )
    run_tick()

    story.pop("grading_pid", None)
    monkeypatch.delenv("PIPELINE_GRADE_XDIST_WORKERS")
    run_tick()

    assert calls[0]["cmd"] == [".venv/bin/python", "-m", "pytest", "-q", "-n", "3"]
    assert calls[1]["cmd"] == [".venv/bin/python", "-m", "pytest", "-q"]
    assert shared == [".venv/bin/python", "-m", "pytest", "-q"]


def test_xdist_workers_ignored_for_non_pytest_command(tmp_path, monkeypatch):
    _story, _manifest_path, calls, run_tick = _make_tick(
        tmp_path,
        monkeypatch,
        live_pids=set(),
        env={"PIPELINE_GRADE_XDIST_WORKERS": "3"},
        test_cmd=["cargo", "test"],
    )

    run_tick()

    assert calls[0]["cmd"] == ["cargo", "test"]


@pytest.mark.parametrize("bad", ["0", "not-a-number"])
def test_invalid_xdist_workers_ignored_with_warning(tmp_path, monkeypatch, caplog, bad):
    _story, _manifest_path, calls, run_tick = _make_tick(
        tmp_path,
        monkeypatch,
        live_pids=set(),
        env={"PIPELINE_GRADE_XDIST_WORKERS": bad},
        test_cmd=[".venv/bin/python", "-m", "pytest", "-q"],
    )

    with caplog.at_level("WARNING"):
        run_tick()

    assert calls[0]["cmd"] == [".venv/bin/python", "-m", "pytest", "-q"]
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "PIPELINE_GRADE_XDIST_WORKERS" in message
    assert bad in message


@pytest.mark.parametrize(
    "pytest_cmd",
    [
        ["/usr/bin/pytest", "-q"],
        ["python", "-m", "pytest", "-q"],
    ],
)
def test_pytest_detection_variants(pytest_cmd):
    assert detached_grade._with_xdist_workers(pytest_cmd, 3) == [
        *pytest_cmd,
        "-n",
        "3",
    ]


@pytest.mark.parametrize(
    "other_cmd",
    [["cargo", "test"], ["python", "-m", "pytest-xdist"]],
)
def test_non_pytest_command_unchanged(other_cmd):
    assert detached_grade._with_xdist_workers(other_cmd, 3) == other_cmd


# --------------------------------------------------------------------------
# Step 5 - live_grade_count probe/scope edge cases.
# --------------------------------------------------------------------------


def test_live_grade_count_counts_live_top_level_manifests_only(tmp_path, monkeypatch):
    """a(live)=1, b(dead)=0, broken(skip), e(zombie -> dead)=0, sub/d(ignored).

    The zombie pid is absent from the patched live set because the real probe
    reports a ``Z``/``Z+`` stat as dead; ``test_is_pid_alive_*`` below pins
    that probe semantics directly. ``sub/d.manifest.json`` must never be seen:
    the glob is non-recursive. Only 1 counts.
    """
    root = tmp_path / "manifests"
    root.mkdir()
    sub = root / "sub"
    sub.mkdir()
    _write_json(root / "a.manifest.json", {"grading_pid": 4242})  # live
    _write_json(root / "b.manifest.json", {"grading_pid": 5555})  # dead
    (root / "broken.manifest.json").write_text("{not json", encoding="utf-8")
    _write_json(root / "e.manifest.json", {"grading_pid": 7777})  # zombie -> dead
    _write_json(sub / "d.manifest.json", {"grading_pid": 6666})  # not top-level
    _write_json(root / "own.manifest.json", {"grading_pid": 9999})  # current story

    monkeypatch.setattr(
        detached_grade, "_is_pid_alive", lambda pid: pid in {4242, 6666, 9999}
    )

    assert (
        detached_grade.live_grade_count(
            root, exclude=root / "own.manifest.json"
        )
        == 1
    )
    # Without the exclusion the current story's own live pid would count too.
    assert detached_grade.live_grade_count(root) == 2


def test_live_grade_count_missing_directory_is_zero(tmp_path):
    assert detached_grade.live_grade_count(tmp_path / "does-not-exist") == 0


def test_is_pid_alive_treats_zombie_and_missing_as_dead(monkeypatch):
    """The probe itself: Z/Z+ stat and ProcessLookupError are dead."""
    import subprocess

    monkeypatch.setattr(detached_grade.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(
        detached_grade.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="Z+ ", stderr=""),
    )
    assert detached_grade._is_pid_alive(7777) is False

    monkeypatch.setattr(
        detached_grade.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="S+ ", stderr=""),
    )
    assert detached_grade._is_pid_alive(7777) is True

    def _missing(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(detached_grade.os, "kill", _missing)
    assert detached_grade._is_pid_alive(5555) is False
