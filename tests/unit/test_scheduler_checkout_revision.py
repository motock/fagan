"""TDD suite: the scheduler daemon must report the revision of the checkout it
was actually imported from, and how far that checkout trails its upstream.

ROOT CAUSE
----------
A merged change is invisible to a running scheduler until its checkout is
pulled AND the process is restarted: ``python -m pipeline.scheduler_daemon``
imports ``pipeline.*`` from the checkout it was started in, so the running code
is that working tree's revision, never the remote's. Observed 2026-09-20: PR
#866 merged as ``0847572``, and a kickstart restart came back up running the
checkout's older ``aa8fefa`` - with the health file reporting ``alive: true``
either way. ``config_fingerprint()`` reported plan_dir / worktree_root /
autonomy / dispatch_backend, none of which can tell a current daemon from a
stale one.

CONTRACT PINNED BY THIS FILE
----------------------------
* A new module-level helper ``_checkout_git_state() -> dict`` in
  ``pipeline/scheduler_daemon.py`` reports ``{"sha": ..., "behind_origin": ...}``
  for the checkout THIS process imported ``pipeline`` from - derived from
  ``pipeline.paths.__file__``, never from the configured ``REPO_ROOT`` (the
  installed scheduler sets ``REPO_ROOT`` to a per-plan sentinel).
* The probe never raises: missing git, a non-repository checkout, an
  unconfigured upstream, a timeout, or unparsable output all leave the
  corresponding field ``None``.
* ``config_fingerprint()`` carries both values as ``checkout_sha`` and
  ``checkout_behind_origin``, placed after ``dispatch_backend`` and before
  ``pid``, and keeps the five previously pinned keys.
* ``health()`` is untouched: its clean-tick key set stays exactly the seven
  pinned keys.

Every test stubs the git boundary (``pipeline.scheduler_daemon.subprocess``)
exactly as ``tests/unit/test_escalation_repo_root.py`` stubs escalation's, so
no real git is ever shelled out and nothing here depends on this machine's
real checkout.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess as real_subprocess
from pathlib import Path

import pytest

from pipeline import paths
from pipeline import scheduler_daemon as sd

# The seven keys health() has always reported on a clean tick. This story must
# not grow that set - the revision fields belong in config_fingerprint().
_PINNED_HEALTH_KEYS = {
    "alive",
    "last_reconcile_ts",
    "last_scan_ts",
    "last_error",
    "reconcile_count",
    "scan_count",
    "reconcile_timed_out",
}

# The five keys config_fingerprint() already reported before this story.
_PINNED_FINGERPRINT_KEYS = {
    "plan_dir",
    "worktree_root",
    "autonomy",
    "dispatch_backend",
    "pid",
}

_SENTINEL_REPO_ROOT = Path("/nonexistent-repo-root-set-per-plan-only")


class _FakeSubprocess:
    """Stand-in for the ``subprocess`` module that records each call.

    ``rev-parse`` yields the sha, every other command yields the behind-count,
    mirroring the two probes ``_checkout_git_state`` makes.
    """

    def __init__(
        self,
        sha: str = "abc1234",
        behind: str = "0",
        rc_head: int = 0,
        rc_behind: int = 0,
        raise_exc: BaseException | None = None,
    ) -> None:
        self.sha = sha
        self.behind = behind
        self.rc_head = rc_head
        self.rc_behind = rc_behind
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def run(self, cmd, **kwargs):
        self.calls.append({"cmd": list(cmd), "cwd": kwargs.get("cwd")})
        if self.raise_exc is not None:
            raise self.raise_exc
        if "rev-parse" in cmd:
            return real_subprocess.CompletedProcess(cmd, self.rc_head, self.sha, "")
        return real_subprocess.CompletedProcess(cmd, self.rc_behind, self.behind, "")


@pytest.fixture
def fake_git(monkeypatch):
    fake = _FakeSubprocess()
    monkeypatch.setattr(sd, "subprocess", fake)
    return fake


def _install_fake(monkeypatch, **kwargs) -> _FakeSubprocess:
    fake = _FakeSubprocess(**kwargs)
    monkeypatch.setattr(sd, "subprocess", fake)
    return fake


def _daemon() -> sd.SchedulerDaemon:
    return sd.SchedulerDaemon(
        reconcile_fn=lambda: None,
        scan_fn=lambda: None,
        bus=object(),
    )


def _helper():
    fn = getattr(sd, "_checkout_git_state", None)
    assert fn is not None, (
        "pipeline.scheduler_daemon._checkout_git_state() -> dict is missing: the "
        "daemon must report the revision of the checkout it imported pipeline "
        "from (and how far it trails its upstream) so a stale scheduler is "
        "distinguishable from a current one"
    )
    return fn


def _expected_checkout() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(paths.__file__)))


def _module_source() -> str:
    return Path(sd.__file__).read_text(encoding="utf-8")


def _helper_segment() -> str:
    """The source of the module-level ``_checkout_git_state`` function."""
    src = _module_source()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_checkout_git_state":
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(
        "pipeline/scheduler_daemon.py has no module-level function "
        "'_checkout_git_state'"
    )


# ---------------------------------------------------------------- the helper


def test_helper_reports_the_sha_and_the_distance_behind_upstream(fake_git):
    assert _helper()() == {"sha": "abc1234", "behind_origin": 0}


def test_helper_probes_the_checkout_the_code_was_imported_from(fake_git):
    _helper()()

    assert fake_git.calls, "the helper made no git call at all"
    expected = _expected_checkout()
    for call in fake_git.calls:
        assert call["cwd"] == expected, (
            "every probe must run in the checkout this process imported "
            "pipeline from"
        )
        assert call["cmd"][0] == "git"


def test_helper_never_consults_repo_root(monkeypatch, fake_git):
    from pipeline import server

    monkeypatch.setattr(server, "REPO_ROOT", _SENTINEL_REPO_ROOT)

    _helper()()

    cwds = {str(call["cwd"]) for call in fake_git.calls}
    assert str(_SENTINEL_REPO_ROOT) not in cwds, (
        "REPO_ROOT is a configured value (a per-plan sentinel under the "
        "installed scheduler), not the tree this code was imported from"
    )


def test_behind_origin_is_an_int_when_upstream_is_ahead(monkeypatch):
    _install_fake(monkeypatch, behind="6")
    state = _helper()()
    assert state["behind_origin"] == 6
    assert isinstance(state["behind_origin"], int)


def test_a_non_repository_or_failed_probe_reports_none(monkeypatch):
    _install_fake(monkeypatch, rc_head=128, rc_behind=128)
    assert _helper()() == {"sha": None, "behind_origin": None}


def test_a_missing_git_binary_reports_none(monkeypatch):
    _install_fake(monkeypatch, raise_exc=FileNotFoundError("git"))
    assert _helper()() == {"sha": None, "behind_origin": None}


def test_a_git_timeout_reports_none(monkeypatch):
    _install_fake(
        monkeypatch,
        raise_exc=real_subprocess.TimeoutExpired(["git", "rev-parse", "HEAD"], 10),
    )
    assert _helper()() == {"sha": None, "behind_origin": None}


def test_an_unconfigured_upstream_leaves_the_sha_but_no_distance(monkeypatch):
    _install_fake(monkeypatch, behind="", rc_behind=128)
    state = _helper()()
    assert state["sha"] == "abc1234"
    assert state["behind_origin"] is None


def test_an_unparsable_count_reports_no_distance(monkeypatch):
    _install_fake(monkeypatch, behind="not-a-number")
    state = _helper()()
    assert state["behind_origin"] is None
    assert state["sha"] == "abc1234"


def test_empty_output_is_reported_as_none(monkeypatch):
    _install_fake(monkeypatch, sha="")
    assert _helper()()["sha"] is None


# ------------------------------------------------------------ the fingerprint


def test_fingerprint_carries_the_checkout_revision(fake_git):
    fingerprint = _daemon().config_fingerprint()

    assert fingerprint["checkout_sha"] == "abc1234"
    assert fingerprint["checkout_behind_origin"] == 0
    for key in _PINNED_FINGERPRINT_KEYS:
        assert key in fingerprint, f"config_fingerprint() dropped {key!r}"
    assert fingerprint["pid"] == os.getpid()

    keys = list(fingerprint)
    assert keys.index("dispatch_backend") < keys.index("checkout_sha")
    assert keys.index("checkout_sha") < keys.index("pid")
    assert keys.index("checkout_sha") < keys.index("checkout_behind_origin")


def test_the_keys_are_present_even_when_git_cannot_answer(monkeypatch):
    _install_fake(monkeypatch, raise_exc=FileNotFoundError("git"))
    fingerprint = _daemon().config_fingerprint()

    assert "checkout_sha" in fingerprint
    assert "checkout_behind_origin" in fingerprint
    assert fingerprint["checkout_sha"] is None
    assert fingerprint["checkout_behind_origin"] is None


def test_revision_survives_the_health_file_round_trip(fake_git, tmp_path):
    path = tmp_path / "health.json"
    _daemon().write_health(str(path))

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["config"]["checkout_sha"] == "abc1234"
    assert payload["config"]["checkout_behind_origin"] == 0


# ----------------------------------------------------------- untouched surface


def test_the_pinned_health_keys_did_not_grow():
    health = _daemon().health()
    assert set(health) == _PINNED_HEALTH_KEYS
    assert "checkout_sha" not in health, (
        "the revision fields belong in config_fingerprint(), not health()"
    )
    assert "checkout_behind_origin" not in health


def test_the_helper_has_a_docstring_naming_the_checkout_contract():
    doc = _helper().__doc__ or ""
    assert doc.strip(), "_checkout_git_state() must be documented"
    assert "checkout" in doc
    assert "REPO_ROOT" in doc


def test_the_helper_is_module_level_in_scheduler_daemon():
    tree = ast.parse(_module_source())
    names = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    assert "_checkout_git_state" in names, (
        "_checkout_git_state must be a module-level function in "
        "pipeline/scheduler_daemon.py"
    )


def test_subprocess_is_imported_after_signal_and_before_sys():
    source = _module_source()
    assert "import subprocess" in source, (
        "`import subprocess` must be added to pipeline/scheduler_daemon.py"
    )
    signal_at = source.index("import signal as _signal_module")
    subprocess_at = source.index("import subprocess")
    sys_at = source.index("\nimport sys")
    assert signal_at < subprocess_at < sys_at, (
        "`import subprocess` must sit directly after `import signal as "
        "_signal_module` (the import list stays alphabetical)"
    )


def test_the_helper_is_defined_immediately_after_the_logger():
    source = _module_source()
    logger_at = source.index("logger = logging.getLogger(__name__)")
    assert "def _checkout_git_state" in source, (
        "_checkout_git_state must be a module-level function in "
        "pipeline/scheduler_daemon.py"
    )
    helper_at = source.index("def _checkout_git_state")
    assert logger_at < helper_at, (
        "_checkout_git_state must be defined after the module logger"
    )
    between = source[logger_at:helper_at]
    assert "def " not in between, (
        "_checkout_git_state must be inserted immediately after "
        "`logger = logging.getLogger(__name__)`"
    )


def test_the_probe_handler_logs_the_error_class_and_never_the_values():
    """The except handler must not be a bare ``pass`` (ruff S110) and must not
    log ``str(exc)``/``repr(exc)`` (which can carry paths or values)."""
    segment = _helper_segment()
    tree = ast.parse(segment)
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
    )
    assert handler.body, "the except handler must not be empty"
    assert not (
        len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass)
    ), "a bare `pass` handler trips ruff S110 (try/except/pass)"
    assert not any(
        isinstance(node, ast.Raise) for node in ast.walk(handler)
    ), "the probe must never raise"
    assert "str(exc)" not in segment
    assert "repr(exc)" not in segment
    assert "logger.debug" in segment, (
        "the handler must log the error CLASS name at debug level"
    )
    assert "type(exc).__name__" in segment, (
        "log the error class name only, never str(exc)/repr(exc)"
    )
    assert "exc_info" not in segment, "no stack trace: debug level only"


def test_the_helper_segment_never_mentions_repo_root_as_a_source():
    """REPO_ROOT is deliberately not consulted; it may only appear in prose."""
    segment = _helper_segment()
    tree = ast.parse(segment)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id != "REPO_ROOT", (
                "REPO_ROOT is a configured value, not the tree this code was "
                "imported from"
            )
        if isinstance(node, ast.Attribute):
            assert node.attr != "REPO_ROOT"
