"""Tests for the .pipeline.env bootstrap in pipeline/__init__.py (CFG env bootstrap).

CONTRACT the implementer must satisfy (see the story brief):

- ``pipeline/__init__.py`` locates the repo root from ``__file__`` (parent of
  the ``pipeline`` package), uses ``pipeline.env_file.find_env_file`` /
  ``parse_env_file`` to read ``<repo_root>/.pipeline.env``, and copies every
  parsed key into ``os.environ``.
- PRECEDENCE: the file WINS over pre-existing ``os.environ`` values, matching
  scripts/dashboard.sh's documented DASHENV-1 behaviour (sourcing overwrites
  caller-exported env). The shell path and the Python path must agree.
- The import-time bootstrap must be INERT under pytest: guarded by BOTH
  ``'pytest' in sys.modules`` and an explicit ``PIPELINE_SKIP_ENV_FILE=1``
  opt-out. A developer's repo-root ``.pipeline.env`` (CFG-D2's
  standalone-setup.sh writes one, e.g. PLAN_DIR) must never leak into a
  pytest run.
- Never raise: a missing, unreadable or malformed file leaves the environment
  untouched and the import proceeds.
- ``__init__.py`` must NOT import ``pipeline.paths`` / ``pipeline.config`` /
  ``pipeline.server`` (circular import) and must not expand ``~``/``$VAR``.

Because the loader is inert under pytest, the import side effect cannot be
observed in-process; the bootstrap body is therefore extracted into a callable
``pipeline._load_env_file(repo_root, environ)`` (environ passed in, so tests
never mutate the real process env via it), which these tests drive directly.
The real end-to-end behaviour is proven via SUBPROCESS runs with
``PIPELINE_SKIP_ENV_FILE`` unset, simulating a launchd-style bare
``python -m pipeline.scheduler_daemon`` launch.

Cleanup: every repo-root ``.pipeline.env`` these tests create is removed (or a
pre-existing developer file restored) in fixture teardown, even on failure —
a leaked file would corrupt every later test run.
"""

import importlib
import inspect
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import pipeline

REPO_ROOT = Path(pipeline.__file__).resolve().parent.parent
ENV_FILE_NAME = ".pipeline.env"
# pipeline/paths.py's default when PLAN_DIR is absent from the env.
DEFAULT_PLAN_DIR = Path("~/.claude/plans").expanduser()

_SUBPROCESS_CODE = "import pipeline.paths; print(pipeline.paths.PLAN_DIR)"


def _fresh_import_pipeline():
    """Re-execute pipeline/__init__.py and return the new module object.

    The previous binding in sys.modules is restored afterwards so other tests
    are unaffected by the swap.
    """
    saved = sys.modules.get("pipeline")
    sys.modules.pop("pipeline", None)
    try:
        return importlib.import_module("pipeline")
    finally:
        if saved is not None:
            sys.modules["pipeline"] = saved
        else:
            sys.modules.pop("pipeline", None)


def _child_env():
    """Env for subprocess children: no PLAN_DIR, no opt-out unless added."""
    return {
        k: v
        for k, v in os.environ.items()
        if k not in ("PLAN_DIR", "PIPELINE_SKIP_ENV_FILE")
    }


@pytest.fixture
def repo_env_file():
    """Install a repo-root .pipeline.env; remove/restore it on teardown.

    Restores a pre-existing developer file byte-for-byte instead of deleting
    it, and registers the target BEFORE writing so even a mid-write failure
    gets cleaned up.
    """
    target = REPO_ROOT / ENV_FILE_NAME
    saved = target.read_bytes() if target.exists() else None
    installed = []

    def _install(content):
        installed.append(target)
        target.write_text(content, encoding="utf-8")
        return target

    yield _install

    if installed:
        if saved is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(saved)


def test_loader_function_exists_with_environ_parameter():
    loader = getattr(pipeline, "_load_env_file", None)
    assert callable(loader), (
        "pipeline/__init__.py must expose _load_env_file(repo_root, environ); "
        "the import-time bootstrap delegates to it so tests can drive the "
        "loader directly (the import side effect is inert under pytest)"
    )
    positional = [
        p
        for p in inspect.signature(loader).parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    assert len(positional) >= 2, (
        "_load_env_file must accept (repo_root, environ) positionally; "
        f"got {list(inspect.signature(loader).parameters)}"
    )


def test_loader_copies_file_keys_into_environ(tmp_path):
    (tmp_path / ENV_FILE_NAME).write_text(
        "# full-line comment\n"
        'export PLAN_DIR="/tmp/plans-from-env"\n'
        "WORKTREE_ROOT='/tmp/wt'\n"
        "A=b=c\n"
        "TILDE=~/.claude/literal\n"
        "\n",
        encoding="utf-8",
    )
    environ = {}
    pipeline._load_env_file(tmp_path, environ)
    assert environ["PLAN_DIR"] == "/tmp/plans-from-env"
    assert environ["WORKTREE_ROOT"] == "/tmp/wt"
    assert environ["A"] == "b=c"  # split on the FIRST '=' only
    # No expansion: ~ and $VAR stay literal (loader must not expand them).
    assert environ["TILDE"] == "~/.claude/literal"


def test_loader_file_wins_over_preexisting_environ(tmp_path):
    """DASHENV-1 parity: the file overwrites caller-exported env values."""
    (tmp_path / ENV_FILE_NAME).write_text(
        "PLAN_DIR=/from-file\n", encoding="utf-8"
    )
    environ = {"PLAN_DIR": "/from-caller", "UNRELATED": "keep"}
    pipeline._load_env_file(tmp_path, environ)
    assert environ["PLAN_DIR"] == "/from-file"
    assert environ["UNRELATED"] == "keep"  # keys not in the file untouched


def test_loader_missing_file_leaves_environ_unchanged(tmp_path):
    environ = {"PLAN_DIR": "/unchanged"}
    pipeline._load_env_file(tmp_path, environ)  # must not raise
    assert environ == {"PLAN_DIR": "/unchanged"}


@pytest.mark.parametrize("mode", ["directory", "undecodable", "no_equals", "empty"])
def test_loader_unreadable_or_malformed_file_leaves_environ_unchanged(
    tmp_path, mode
):
    target = tmp_path / ENV_FILE_NAME
    if mode == "directory":
        target.mkdir()
    elif mode == "undecodable":
        target.write_bytes(b"\xff\xfe\x81\x81")
    elif mode == "no_equals":
        target.write_text("just a line\nanother line\n", encoding="utf-8")
    else:
        target.write_text("", encoding="utf-8")
    environ = {"PLAN_DIR": "/unchanged"}
    pipeline._load_env_file(tmp_path, environ)  # must not raise
    assert environ == {"PLAN_DIR": "/unchanged"}


def test_import_inert_under_pytest(repo_env_file, tmp_path):
    """With 'pytest' in sys.modules, importing pipeline injects nothing."""
    assert "pytest" in sys.modules  # precondition: this is the guard's trigger
    plan_dir = tmp_path / "plans" / "from-env-file"
    repo_env_file(f"PLAN_DIR={plan_dir}\nWORKTREE_ROOT={tmp_path / 'wt'}\n")
    # Strip the opt-out so the pytest-modules guard is the ONLY active guard.
    os.environ.pop("PIPELINE_SKIP_ENV_FILE", None)
    before = os.environ.get("PLAN_DIR")
    module = _fresh_import_pipeline()
    assert module is not None  # the import itself proceeds
    assert os.environ.get("PLAN_DIR", before) == before
    assert os.environ.get("PLAN_DIR") != str(plan_dir)


def test_import_inert_via_opt_out(repo_env_file, tmp_path):
    """With PIPELINE_SKIP_ENV_FILE=1 the loader does nothing."""
    plan_dir = tmp_path / "plans" / "optout"
    repo_env_file(f"PLAN_DIR={plan_dir}\n")
    os.environ["PIPELINE_SKIP_ENV_FILE"] = "1"
    before = os.environ.get("PLAN_DIR")
    module = _fresh_import_pipeline()
    assert module is not None
    assert os.environ.get("PLAN_DIR", before) == before
    assert os.environ.get("PLAN_DIR") != str(plan_dir)


def test_subprocess_bootstrap_picks_up_env_file(repo_env_file, tmp_path):
    """The real proof: a bare-python launch (launchd-style) reads the file.

    cwd is the repo root, PIPELINE_SKIP_ENV_FILE is unset, PLAN_DIR is absent
    from the child env — the printed PLAN_DIR must come from .pipeline.env.
    """
    plan_dir = tmp_path / "plans" / "subprocess"
    repo_env_file(f"PLAN_DIR={plan_dir}\n")
    child_env = _child_env()
    assert "PLAN_DIR" not in child_env
    assert "PIPELINE_SKIP_ENV_FILE" not in child_env
    proc = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_CODE],
        cwd=str(REPO_ROOT),
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip()) == plan_dir


def test_subprocess_opt_out_prints_default(repo_env_file, tmp_path):
    """Negative: PIPELINE_SKIP_ENV_FILE=1 -> the default path is printed."""
    plan_dir = tmp_path / "plans" / "optout-subprocess"
    repo_env_file(f"PLAN_DIR={plan_dir}\n")
    child_env = _child_env()
    child_env["PIPELINE_SKIP_ENV_FILE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_CODE],
        cwd=str(REPO_ROOT),
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip()) == DEFAULT_PLAN_DIR
    assert Path(proc.stdout.strip()) != plan_dir


def test_init_source_wires_guards_precedence_and_no_circular_imports():
    """Mechanically grade the __init__.py requirements at source level."""
    source = Path(pipeline.__file__).read_text(encoding="utf-8")
    assert "__file__" in source  # repo root derived from __file__
    assert "find_env_file" in source and "parse_env_file" in source
    assert "_load_env_file" in source  # extracted callable the tests drive
    assert "PIPELINE_SKIP_ENV_FILE" in source  # explicit opt-out guard
    assert "pytest" in source  # 'pytest' in sys.modules guard
    assert "precedence" in source.lower()  # file-wins precedence documented
    # DO NOT: no circular imports of paths/config/server from __init__.py.
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert not re.search(r"\b(paths|config|server)\b", stripped), (
                f"pipeline/__init__.py must not import paths/config/server: "
                f"{stripped!r}"
            )