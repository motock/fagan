"""TDD tests: the merge re-verify child grades the plan in the story's namespace.

``start_merge_reverify`` spawns ``python -m pipeline.merge_reverify`` as a
detached process.  The child starts with nothing bound, so without the
namespace it reads the default partition, finds no manifest for a team plan,
and reports ``merge re-verify raised FileNotFoundError``.  The spawner now
passes the tick's namespace as ``--namespace=<value>`` and the child binds it
before it reads the plan, the same way ``pipeline/review_job.py`` does.

Only the process boundary (``subprocess.Popen`` / ``subprocess.run``), the
verdict seams in ``pipeline.server`` and the record store are stubbed; the
store is a namespace-aware fake, because ``SqliteStore`` serves only the
default namespace.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# isort: off
# pipeline.server is imported first on purpose: pre-existing import cycle
# (tests/unit/test_triage_cap_silent_skip.py uses the same order).
import pipeline.server  # noqa: F401
from pipeline import server
import pipeline.merge_reverify as mr
# isort: on

from pipeline.actor import bind_namespace
from pipeline.namespace_guard import NAMESPACE_MALFORMED, NAMESPACE_MISMATCH

TEAM = "acme/web"
PLAN = "P"
KEY = "S-1"


class _Proc:
    pid = 4242


class _Completed:
    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def _fake_popen(monkeypatch) -> dict:
    calls: dict = {}

    def _popen(argv, **kwargs):
        calls["argv"] = argv
        return _Proc()

    monkeypatch.setattr(mr.subprocess, "Popen", _popen)
    return calls


def _start(tmp_path: Path, **kwargs) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir(exist_ok=True)
    mr.start_merge_reverify(PLAN, KEY, str(worktree), tmp_path / "sr", **kwargs)


class _Records:
    """A namespace-aware record view: manifests keyed by ``(namespace, plan)``."""

    def __init__(self) -> None:
        self._manifests: dict[tuple[str, str], dict] = {}

    def put(self, namespace: str, plan: str, manifest: dict) -> None:
        self._manifests[(namespace, plan)] = manifest

    def get_manifest(self, namespace: str, plan_name: str) -> dict | None:
        return self._manifests.get((namespace, plan_name))


class _Store:
    def __init__(self) -> None:
        self._records = _Records()

    def records(self) -> _Records:
        return self._records


def _store(namespace: str, plan: str = PLAN) -> _Store:
    """A store that holds *plan* only under *namespace*."""
    store = _Store()
    story = {"status": "merging", "last_test_check": "2026-10-01T00:00:00Z"}
    store.records().put(namespace, plan, {"epics": {}, "stories": {KEY: story}})
    return store


def _patch_head(monkeypatch, sha: str) -> None:
    monkeypatch.setattr(
        mr.subprocess, "run", lambda argv, **kwargs: _Completed(stdout=sha + "\n")
    )


def _pass_seams(monkeypatch) -> None:
    monkeypatch.setattr(
        server,
        "_reverify_acceptance",
        lambda story, path, key, settings=None: {"state": "pass", "error": ""},
    )
    monkeypatch.setattr(
        server, "_reverify_build", lambda path, settings=None: {"state": "pass", "error": ""}
    )


def _run(tmp_path: Path, *args: str) -> tuple[int, dict | None]:
    worktree = tmp_path / "wt"
    worktree.mkdir(exist_ok=True)
    result_path = tmp_path / "result.json"
    rc = mr.main([PLAN, KEY, str(worktree), str(result_path), *args])
    result = json.loads(result_path.read_text()) if result_path.exists() else None
    return rc, result


# ---------------------------------------------------------------------------
# The spawner hands the tick's namespace to the child
# ---------------------------------------------------------------------------


def test_start_passes_the_bound_namespace_as_a_flag(tmp_path, monkeypatch):
    calls = _fake_popen(monkeypatch)

    with bind_namespace(TEAM):
        _start(tmp_path)

    assert calls["argv"][-1] == f"--namespace={TEAM}"


def test_start_omits_the_namespace_flag_for_the_default_namespace(tmp_path, monkeypatch):
    calls = _fake_popen(monkeypatch)

    _start(tmp_path)

    assert not any(arg.startswith("--namespace=") for arg in calls["argv"])


def test_start_keeps_lint_last_for_the_default_namespace(tmp_path, monkeypatch):
    calls = _fake_popen(monkeypatch)

    _start(tmp_path, with_lint=True)

    assert calls["argv"][-1] == "--lint"
    assert not any(arg.startswith("--namespace=") for arg in calls["argv"])


def test_start_puts_lint_before_the_namespace_flag(tmp_path, monkeypatch):
    calls = _fake_popen(monkeypatch)

    with bind_namespace(TEAM):
        _start(tmp_path, with_lint=True)

    assert calls["argv"][-2:] == ["--lint", f"--namespace={TEAM}"]


# ---------------------------------------------------------------------------
# The child binds the namespace before it reads the plan
# ---------------------------------------------------------------------------


def test_main_grades_a_plan_held_only_in_the_requested_namespace(tmp_path, monkeypatch):
    # Reproducing test: before the fix the child reads the default partition,
    # finds no manifest and writes state "error" with sha None.
    monkeypatch.setattr(server, "_store", _store(TEAM))
    _patch_head(monkeypatch, "cafe1234")
    _pass_seams(monkeypatch)

    rc, result = _run(tmp_path, f"--namespace={TEAM}")

    assert rc == 0
    assert result is not None
    assert result["state"] == "pass"
    assert result["error"] == ""
    assert result["sha"] == "cafe1234"


def test_main_without_the_flag_reads_the_default_namespace(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_store", _store("default"))
    _patch_head(monkeypatch, "cafe1234")
    _pass_seams(monkeypatch)

    rc, result = _run(tmp_path)

    assert rc == 0
    assert result is not None
    assert result["state"] == "pass"
    assert result["sha"] == "cafe1234"


def test_main_refuses_a_namespace_that_does_not_hold_the_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_store", _store("default"))
    _patch_head(monkeypatch, "cafe1234")
    _pass_seams(monkeypatch)

    rc, result = _run(tmp_path, f"--namespace={TEAM}")

    assert rc == 1
    assert result is not None
    assert result["state"] == "error"
    assert result["error"] == NAMESPACE_MISMATCH
    assert result["sha"] is None


@pytest.mark.parametrize("flag", ["--namespace=../x", "--namespace="])
def test_main_refuses_a_malformed_namespace_without_a_pass(tmp_path, monkeypatch, flag):
    monkeypatch.setattr(server, "_store", _store("default"))
    _patch_head(monkeypatch, "cafe1234")
    _pass_seams(monkeypatch)

    rc, result = _run(tmp_path, flag)

    assert rc == 1
    assert result is not None
    assert result["state"] == "error"
    assert result["error"] == NAMESPACE_MALFORMED


def test_main_refuses_an_unknown_extra_argument(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_store", _store("default"))
    _patch_head(monkeypatch, "cafe1234")
    _pass_seams(monkeypatch)

    rc, result = _run(tmp_path, "--bogus")

    assert rc == 1
    assert result is None
