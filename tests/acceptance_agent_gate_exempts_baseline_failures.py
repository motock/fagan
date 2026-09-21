"""Acceptance: the agent-side full-suite done-gate must exempt failures that
were already failing before the story touched the tree.

`_full_suite_result` re-runs a red suite once (FSU-01) before rejecting `done`.
A reproducible failure still rejects - correctly, when the story introduced it.
But on a repo with pre-existing, unrelated failures a rework round can never
produce a green suite, so it rejects every `done` until the suite-reject cap
parks it, even though the tick that grades that same round exempts exactly
those failures (`pipeline.story_status._baseline_exempted_failures`).

The exemption must mirror that tick-side helper's semantics exactly, and fail
closed on every case it cannot justify:
  - no marker at all (the common first dispatch),
  - a legacy "ok" marker written before the ids were recorded,
  - a baseline that recorded no ids (a green baseline, or an unparseable one),
  - any run failure the baseline does not also name.
Only a non-empty run whose failures are ALL in the baseline may pass.

External boundaries (subprocess.run / detect_test_command / detect_lint_command)
are mocked; the run's own failing node ids are parsed by the REAL
`pipeline.build_detect.failed_node_ids` reached through the module's own `p`,
so the wiring that exposes it is graded too - not just the comparison.
"""
import importlib.util
import os
from pathlib import Path

import pytest

os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_MARKER = ".dispatch_baseline_test_checked"
_BASELINE_FAILURE = "FAILED tests/a.py::test_x - assert False"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


la = _load("local_agent", _SCRIPTS / "local_agent.py")
lao = _load("local_agent_oracle", _SCRIPTS / "local_agent_oracle.py")
BOTH = [la, lao]


class _R:
    def __init__(self, rc, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def _suite(monkeypatch, mod, cwd, returncodes, stdout):
    """Wire the suite gate's boundaries. Returns the recorded call list."""
    calls = []
    pending = list(returncodes)

    def _run(argv, **kw):
        calls.append(argv)
        return _R(pending.pop(0) if pending else returncodes[-1], out=stdout)

    monkeypatch.setattr(mod, "CWD", cwd)
    monkeypatch.setattr(mod.p, "detect_test_command", lambda c: (str(cwd), ["pytest", "-q"]))
    monkeypatch.setattr(mod.p, "detect_lint_command", lambda c: None)
    monkeypatch.setattr(mod.p, "_is_heavy", lambda argv: False)
    monkeypatch.setattr(mod.subprocess, "run", _run)
    return calls


def _marker(tmp_path, payload):
    if payload is not None:
        (tmp_path / _MARKER).write_text(payload)
    return tmp_path


@pytest.mark.parametrize("mod", BOTH)
def test_baseline_only_failure_does_not_reject_done(monkeypatch, mod, tmp_path):
    """Both runs red, but the only failure is one the baseline already had."""
    _marker(tmp_path, '{"failed_node_ids": ["tests/a.py::test_x"]}')
    calls = _suite(monkeypatch, mod, tmp_path, [1, 1], _BASELINE_FAILURE)

    assert mod._full_suite_result() == (True, "", None)
    assert len(calls) == 2  # still re-run once before exempting


@pytest.mark.parametrize("mod", BOTH)
def test_a_failure_the_baseline_does_not_name_still_rejects(monkeypatch, mod, tmp_path):
    """The story's own new failure must still reject, even alongside a
    pre-existing one - a partial match exempts nothing."""
    _marker(tmp_path, '{"failed_node_ids": ["tests/a.py::test_x"]}')
    _suite(
        monkeypatch, mod, tmp_path, [1, 1],
        _BASELINE_FAILURE + "\nFAILED tests/b.py::test_new - assert False",
    )

    ok, tail, gate = mod._full_suite_result()

    assert ok is False
    assert gate == "test"
    assert "test_new" in tail


@pytest.mark.parametrize("mod", BOTH)
def test_no_marker_still_rejects(monkeypatch, mod, tmp_path):
    """A fresh dispatch has no baseline on disk: unchanged behavior."""
    _suite(monkeypatch, mod, tmp_path, [1, 1], _BASELINE_FAILURE)

    ok, _, gate = mod._full_suite_result()

    assert ok is False
    assert gate == "test"


@pytest.mark.parametrize("mod", BOTH)
def test_legacy_marker_still_rejects(monkeypatch, mod, tmp_path):
    """Markers written before the ids were recorded hold "ok" and nothing
    else: they must exempt nothing rather than crash the gate."""
    _marker(tmp_path, "ok\n")
    _suite(monkeypatch, mod, tmp_path, [1, 1], _BASELINE_FAILURE)

    ok, _, gate = mod._full_suite_result()

    assert ok is False
    assert gate == "test"


@pytest.mark.parametrize("mod", BOTH)
def test_baseline_that_recorded_no_ids_still_rejects(monkeypatch, mod, tmp_path):
    """A green - or unparseable - baseline recorded an empty list. Every
    current failure is then the story's own."""
    _marker(tmp_path, '{"failed_node_ids": []}')
    _suite(monkeypatch, mod, tmp_path, [1, 1], _BASELINE_FAILURE)

    ok, _, gate = mod._full_suite_result()

    assert ok is False
    assert gate == "test"


@pytest.mark.parametrize("mod", BOTH)
def test_a_green_retry_is_still_exempted_without_a_baseline(monkeypatch, mod, tmp_path):
    """Control: FSU-01's retry-once must survive this change untouched."""
    calls = _suite(monkeypatch, mod, tmp_path, [1, 0], _BASELINE_FAILURE)

    assert mod._full_suite_result() == (True, "", None)
    assert len(calls) == 2
