"""Fresh-clone hygiene: a bare ``pytest -q`` must behave like CI.

Grades the three test-infrastructure fixes this story makes (no production
file may change), each of which is currently absent:

1. ``tests/conftest.py`` defaults ``AGENTS_DIR`` to the repo's checked-in
   personas (``agents/``) the way CI does - via ``setdefault``, so an explicit
   ``AGENTS_DIR`` still wins.
2. ``_finish_if_green_spy`` (tests/unit/_local_agent_oracle_test_helpers.py)
   stubs ``write_done_marker``, so a green ``finish_if_green`` cannot leak
   ``.agent_done`` into the process CWD (the repo root of a bare run).
3. ``tests/conftest.py`` grows controller-only ``pytest_sessionstart`` /
   ``pytest_sessionfinish`` hooks plus a root-entry helper that fails the run
   when it creates NEW untracked top-level repo-root entries.

Contract this suite pins on ``tests/conftest.py``:

* a module-level callable (``_untracked_root_entries``) returning the
  untracked top-level repo-root entries from ``git status --porcelain
  --untracked-files=normal`` - the ``?? `` lines whose path has no ``/`` except
  an optional trailing one - and an empty set when the git call raises
  ``OSError``.
* ``pytest_sessionstart(session)`` / ``pytest_sessionfinish(session,
  exitstatus)``: no-ops while ``session.config`` has ``workerinput`` (an xdist
  worker); otherwise record at start, compare at finish, and on a NEW entry
  print an ERROR naming it and set ``session.exitstatus = 1``.
"""
import importlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from tests.unit._local_agent_oracle_test_helpers import _finish_if_green_spy, lao

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFTEST_PATH = REPO_ROOT / "tests" / "conftest.py"
_REAL_RUN = subprocess.run

# A canned `git status --porcelain --untracked-files=normal` listing covering
# every line shape the conftest's filter must tell apart.
_CANNED_STATUS = (
    " M pipeline/paths.py\n"                             # tracked, modified
    "M  scripts/local_agent_oracle.py\n"                  # tracked, staged
    "?? tests/unit/test_fresh_clone_test_hygiene.py\n"    # nested untracked
    "?? untracked_dir/nested.txt\n"                       # nested untracked
    "?? untracked_dir/\n"                                 # top-level untracked dir
    "?? brand_new_root_litter.txt\n"                      # top-level untracked file
)

# Re-executes tests/conftest.py in a child process with AGENTS_DIR preset or
# removed, then prints the value the conftest left behind.
_CHILD = """
import os, sys
conftest = sys.argv[1]
if len(sys.argv) > 2:
    os.environ["AGENTS_DIR"] = sys.argv[2]
else:
    os.environ.pop("AGENTS_DIR", None)
ns = {"__file__": conftest, "__name__": "conftest_under_test"}
exec(compile(open(conftest).read(), conftest, "exec"), ns)
print(os.environ.get("AGENTS_DIR"))
"""


def _conftest_module():
    """The tests/conftest.py module pytest itself loaded, located by __file__
    so the hooks driven below are the objects this run uses - never a second,
    re-executed copy of the module."""
    target = str(CONFTEST_PATH.resolve())
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if f and str(Path(f).resolve()) == target:
            return mod
    return importlib.import_module("tests.conftest")


def _root_entry_helper(conftest):
    """The conftest's untracked-root-entry helper: ``_untracked_root_entries``
    by preference, else any module-level callable whose name says both
    'untracked' and 'root' - the behavior is graded, not the spelling."""
    pinned = getattr(conftest, "_untracked_root_entries", None)
    if callable(pinned):
        return pinned
    for name in sorted(vars(conftest)):
        if "untracked" in name and "root" in name and callable(getattr(conftest, name)):
            return getattr(conftest, name)
    raise AssertionError(
        "tests/conftest.py must define a module-level helper "
        "(`_untracked_root_entries`) returning the untracked top-level "
        "repo-root entries from `git status --porcelain "
        "--untracked-files=normal`"
    )


class _CannedStdout(str):
    """str that also survives .decode(), so it works whether the conftest asks
    git for text or for bytes."""

    def decode(self, *args, **kwargs):
        return str(self)


class _FakeGit:
    """Stand-in for the conftest module's ``subprocess`` namespace: run()
    returns a result whose .stdout is the canned git listing (or raises)."""

    def __init__(self, stdout="", exc=None):
        self.stdout = stdout
        self.exc = exc
        self.calls = []

    def run(self, *args, **kwargs):
        self.calls.append(args)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(stdout=_CannedStdout(self.stdout), stderr="", returncode=0)

    def check_output(self, *args, **kwargs):
        self.calls.append(args)
        if self.exc is not None:
            raise self.exc
        return _CannedStdout(self.stdout)


def _patch_git(monkeypatch, conftest, fake):
    """Intercept every route the conftest could take to git: its own
    ``subprocess`` module object, a ``from subprocess import run`` binding,
    and the shared subprocess module itself."""
    monkeypatch.setattr(conftest, "subprocess", fake, raising=False)
    monkeypatch.setattr(conftest, "run", fake.run, raising=False)
    monkeypatch.setattr(subprocess, "run", fake.run)


class _Session:
    """Minimal pytest session: a config whose ``workerinput`` presence answers
    hasattr() the way an xdist controller (absent) and worker (present) do,
    plus a settable exitstatus. Any other attribute read yields a permissive
    stand-in, so a hook inspecting more than this still runs."""

    def __init__(self, worker=False):
        self._worker = worker
        self.config = self
        self.exitstatus = None

    def __getattr__(self, name):
        if name == "workerinput" and not self._worker:
            raise AttributeError(name)
        return _Session()


def _entry_names(entries):
    """Normalise the helper's return value to bare repo-root entry names,
    tolerating whether it keeps the '?? ' prefix or a trailing slash; a nested
    path collapses to its basename, which the 'ignored' assertions then
    require to be absent."""
    names = set()
    for entry in entries:
        text = str(entry).strip()
        if text.startswith("??"):
            text = text[2:].strip()
        names.add(text.rstrip("/").rsplit("/", 1)[-1])
    return names


def _with_restored_globals(conftest, fn):
    """Snapshot/restore the conftest module's globals around direct hook
    calls, so a start-snapshot a hook records in a module global for THIS
    test can never replace the one the real session recorded at its start."""
    snapshot = dict(vars(conftest))
    try:
        fn()
    finally:
        for key, value in snapshot.items():
            if key not in ("subprocess", "run"):  # monkeypatch undoes these
                setattr(conftest, key, value)


def test_conftest_defaults_agents_dir_to_repo_personas(monkeypatch):
    conftest = _conftest_module()
    monkeypatch.delenv("AGENTS_DIR", raising=False)
    _with_restored_globals(conftest, lambda: importlib.reload(conftest))
    raw = os.environ.get("AGENTS_DIR")
    assert raw, (
        "tests/conftest.py must default AGENTS_DIR to the repo personas the "
        "way CI does (os.environ.setdefault); it is unset in this run"
    )
    agents = Path(raw).expanduser().resolve()
    assert agents == (REPO_ROOT / "agents").resolve()
    assert (agents / "code-reviewer.md").is_file(), (
        "the default AGENTS_DIR must be the repo's checked-in personas"
    )


def test_agents_dir_default_is_a_setdefault(tmp_path):
    """An explicit AGENTS_DIR must survive the conftest import (setdefault,
    not an unconditional assignment); an unset one must default to agents/."""
    explicit = str(tmp_path / "explicit-agents")
    for preset, expected in ((explicit, explicit), (None, str(REPO_ROOT / "agents"))):
        env = dict(os.environ)
        env.pop("AGENTS_DIR", None)
        argv = [sys.executable, "-c", _CHILD, str(CONFTEST_PATH)]
        if preset:
            env["AGENTS_DIR"] = preset
            argv.append(preset)
        proc = _REAL_RUN(argv, env=env, capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stderr
        assert Path(proc.stdout.strip()).resolve() == Path(expected).resolve()


def test_finish_if_green_spy_does_not_leak_done_marker(tmp_path, monkeypatch):
    # Snapshot first: while the fix is absent OTHER tests leak this marker into
    # the repo root, and this test must fail only on litter IT creates - not on
    # theirs. With the fix in place the snapshot is False, so the final assert
    # is exactly "the repo-root marker does not exist".
    root_marker = REPO_ROOT / ".agent_done"
    root_marker_existed = root_marker.exists()
    messages, commits, _full_calls = _finish_if_green_spy(
        monkeypatch, oracle_ok=True, full_ok=True
    )
    monkeypatch.setattr(lao, "CWD", tmp_path)
    assert lao.finish_if_green(1, messages=messages) is True
    assert commits, "the green path must have reached the auto-commit step"
    assert not (tmp_path / ".agent_done").exists(), (
        "the spy must stub write_done_marker: a green finish_if_green may not "
        "write .agent_done into the process CWD"
    )
    assert root_marker.exists() == root_marker_existed, (
        "a green finish_if_green must not write .agent_done into the repo root"
    )


def test_done_marker_write_is_observable(tmp_path, monkeypatch):
    """Control for the leak test: with CWD pointed at tmp_path a REAL
    write_done_marker call does land there, so that test's assertion on the
    same path is not vacuous."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    lao.write_done_marker(0)
    assert (tmp_path / ".agent_done").exists()


def test_root_entry_helper_returns_empty_set_when_git_is_missing(monkeypatch):
    conftest = _conftest_module()
    for exc in (FileNotFoundError("git"), OSError("no git")):
        _patch_git(monkeypatch, conftest, _FakeGit(exc=exc))
        assert set(_root_entry_helper(conftest)()) == set(), (
            f"a failed git call ({type(exc).__name__}) must be swallowed and "
            f"reported as no entries"
        )


def test_root_entry_helper_reports_only_top_level_untracked(monkeypatch):
    conftest = _conftest_module()
    fake = _FakeGit(stdout=_CANNED_STATUS)
    _patch_git(monkeypatch, conftest, fake)
    names = _entry_names(_root_entry_helper(conftest)())
    assert fake.calls, "the entries must come from a git call"
    flat = [str(arg) for call in fake.calls for arg in call]
    assert any("status" in arg for arg in flat) and any("porcelain" in arg for arg in flat)
    assert "brand_new_root_litter.txt" in names
    assert "untracked_dir" in names, (
        "an untracked top-level directory (a '?? ' entry whose only slash is "
        "the trailing one) is itself a repo-root entry and must be reported"
    )
    for ignored in ("paths.py", "local_agent_oracle.py", "nested.txt",
                    "test_fresh_clone_test_hygiene.py"):
        assert ignored not in names, (
            f"tracked/modified files and nested untracked paths must be "
            f"ignored, not reported as repo-root entries: {ignored}"
        )


def test_root_entry_helper_empty_listing_yields_no_entries(monkeypatch):
    conftest = _conftest_module()
    _patch_git(monkeypatch, conftest, _FakeGit(stdout=""))
    assert not _root_entry_helper(conftest)()


def test_session_hooks_exist_and_skip_in_xdist_worker(monkeypatch):
    conftest = _conftest_module()
    for name in ("pytest_sessionstart", "pytest_sessionfinish"):
        assert callable(getattr(conftest, name, None)), (
            f"tests/conftest.py must define the {name} hook that fails a run "
            f"littering the repo root"
        )
    fake = _FakeGit(exc=AssertionError("git must not run in an xdist worker session"))
    _patch_git(monkeypatch, conftest, fake)
    worker = _Session(worker=True)

    def _drive():
        conftest.pytest_sessionstart(worker)
        conftest.pytest_sessionfinish(worker, 0)

    _with_restored_globals(conftest, _drive)
    assert not fake.calls, "an xdist worker session must not run the git check"
    assert worker.exitstatus != 1


def test_session_hooks_run_git_in_controller(monkeypatch):
    """Positive control for the worker-skip test: in a controller session the
    hooks DO consult git, so the skip test's assertion is not vacuous."""
    conftest = _conftest_module()
    fake = _FakeGit(stdout=_CANNED_STATUS)
    _patch_git(monkeypatch, conftest, fake)
    session = _Session()

    def _drive():
        conftest.pytest_sessionstart(session)
        conftest.pytest_sessionfinish(session, 0)

    _with_restored_globals(conftest, _drive)
    assert fake.calls, "a controller session must run the git check"


def test_session_finish_fails_run_on_new_root_litter(monkeypatch, capsys):
    conftest = _conftest_module()
    fake = _FakeGit(stdout=_CANNED_STATUS)
    _patch_git(monkeypatch, conftest, fake)
    session = _Session()

    def _drive():
        conftest.pytest_sessionstart(session)
        fake.stdout = _CANNED_STATUS + "?? late_litter.txt\n"
        conftest.pytest_sessionfinish(session, 0)

    _with_restored_globals(conftest, _drive)
    assert session.exitstatus == 1, "a NEW untracked repo-root entry must fail the run"
    printed = capsys.readouterr()
    assert "ERROR" in (printed.out + printed.err)
    assert "late_litter.txt" in (printed.out + printed.err), (
        "the failure must name the new untracked repo-root entry"
    )


def test_session_finish_clean_when_no_new_root_litter(monkeypatch, capsys):
    conftest = _conftest_module()
    _patch_git(monkeypatch, conftest, _FakeGit(stdout=_CANNED_STATUS))
    session = _Session()

    def _drive():
        conftest.pytest_sessionstart(session)
        conftest.pytest_sessionfinish(session, 0)

    _with_restored_globals(conftest, _drive)
    assert session.exitstatus != 1, (
        "entries that already existed at session start are not new and must "
        "not fail the run"
    )
    printed = capsys.readouterr()
    assert "ERROR" not in (printed.out + printed.err)