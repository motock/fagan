"""OPSA-8: the standalone acceptance-fixture lint helper and its ingest wiring.

Contract graded here (the story brief):

* ``pipeline/fixture_lint.py`` exposes
  ``lint_acceptance_source(source: str, repo_root: Path) -> list[str]``.  It
  materialises ``source`` into a temp file under a temp dir, runs ``ruff check``
  against it (the repo's pinned ``ruff==0.16.5``, honouring the repo's ruff
  config) and returns ruff's violation lines.  An empty list means clean.  The
  fixture is NEVER imported or executed - lint text only.
* Tool failure is FAIL CLOSED: a missing binary, a crash or a timeout raises a
  clear error naming the cause instead of silently reporting "clean".
* ``pipeline.ingest`` rejects a story whose ``.py`` acceptance entry fails lint,
  naming the entry path and the exact violations.  Non-``.py`` entries are
  skipped.
* The module is standalone (no reference to ``pipeline.server`` /
  ``pipeline.ingest``) so the sibling OPSA-7 ``patch_acceptance`` executor can
  import the same helper.

NOTE FOR THE REVIEWER: this file deliberately pins the brief above.  The
committed ``tests/unit/test_ingest_fixture_lint.py`` on this branch pins the
OPPOSITE resolution (delete ``pipeline/fixture_lint.py``, wire
``build_detect._lint_acceptance_fixtures``, fail OPEN when ruff is absent).  The
two cannot both be green; the brief is authoritative for this dispatch.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "pipeline" / "fixture_lint.py"
REQUIREMENTS_DEV = REPO_ROOT / "requirements-dev.txt"
PINNED_RUFF = "0.16.5"

CLEAN_SOURCE = '''\
"""A clean acceptance fixture."""


def test_ok() -> None:
    assert 1 + 1 == 2
'''

# The brief's headline failure class: an unused variable.  RUF059 is the
# unused-unpacked-variable rule, so it only fires when the repo's ruff config
# selects the RUF rules.
RUF059_SOURCE = '''\
"""Fixture with an unused unpacked variable (RUF059)."""


def test_unpack() -> None:
    first, second = (1, 2)
    assert first == 1
'''

F401_SOURCE = '''\
"""Fixture with an unused import (F401)."""

import pytest


def test_acceptance() -> None:
    assert 1 + 1 == 2
'''

F841_SOURCE = '''\
"""Fixture with an assigned-but-unused local (F841)."""


def test_unused() -> None:
    unused_value = 42
    assert 1 + 1 == 2
'''

RUF059_LINE = "RUF059 Unused unpacked variable: `unused_value`"
F401_LINE = "F401 `pytest` imported but unused"
F841_LINE = "F841 Local variable `unused_value` is assigned to but never used"

# A ruff that is on PATH so ``shutil.which("ruff")`` resolves; the recorder
# below intercepts the actual subprocess call.
FAKE_RUFF_SCRIPT = """#!/bin/sh
for a in "$@"; do
  if [ "$a" = "--version" ]; then echo "ruff 0.16.5"; exit 0; fi
done
exit 0
"""


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def fl():
    """The module under test.  Missing module -> import error (correct RED)."""
    import pipeline.fixture_lint as mod

    return mod


@pytest.fixture(scope="module")
def lint(fl):
    return fl.lint_acceptance_source


@pytest.fixture
def plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private PLAN_DIR, patched on every module that binds it."""
    from pipeline import concurrency as pcon
    from pipeline import persistence as ppers
    from pipeline import server as p

    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


# --------------------------------------------------------------------------- #
# ruff invocation recorder
# --------------------------------------------------------------------------- #
_REAL_RUN = subprocess.run
_REAL_CHECK_OUTPUT = subprocess.check_output


class _RuffRecorder:
    """Records how the helper invoked ruff and fakes ruff's output.

    The repo's own style is ``import subprocess`` + ``subprocess.run(...)``
    (see ``pipeline/build_detect.py``), so patching ``subprocess.run`` /
    ``subprocess.check_output`` intercepts the call regardless of how the
    helper resolved the binary.
    """

    def __init__(
        self,
        stdout: str = "",
        returncode: int = 0,
        stderr: str = "",
        exc: BaseException | None = None,
        version: str = PINNED_RUFF,
        missing: bool = False,
    ) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.exc = exc
        self.version = version
        self.missing = missing
        self.calls: list[dict] = []

    @staticmethod
    def _is_ruff(cmd) -> bool:
        if isinstance(cmd, str):
            return "ruff" in cmd
        return any(Path(str(part)).name.startswith("ruff") for part in cmd)

    def __call__(self, cmd, *args, **kwargs):
        if not self._is_ruff(cmd):
            return _REAL_RUN(cmd, *args, **kwargs)
        if self.missing:
            raise FileNotFoundError("ruff: command not found")
        if self.exc is not None:
            raise self.exc
        cmd = list(cmd)
        entry: dict = {"cmd": cmd, "kwargs": dict(kwargs)}
        target = str(cmd[-1])
        entry["target"] = target
        if os.path.exists(target):
            entry["content"] = Path(target).read_text()
        self.calls.append(entry)
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, f"ruff {self.version}\n", "")
        return subprocess.CompletedProcess(
            cmd, self.returncode, self.stdout, self.stderr
        )

    def check_output(self, cmd, *args, **kwargs):
        proc = self(cmd, *args, **kwargs)
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode, cmd, proc.stdout, proc.stderr
            )
        return proc.stdout


def _write_script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _fake_ruff(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _RuffRecorder,
    tmp_path: Path,
    *,
    hide_ruff: bool = False,
) -> _RuffRecorder:
    """Intercept ruff: patch the subprocess call and the PATH lookup."""
    monkeypatch.setattr(subprocess, "run", recorder)
    monkeypatch.setattr(subprocess, "check_output", recorder.check_output)
    real_which = shutil.which
    script = _write_script(tmp_path / "fakebin" / "ruff", FAKE_RUFF_SCRIPT)

    def fake_which(name, *args, **kwargs):
        if name == "ruff":
            return None if hide_ruff else str(script)
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", fake_which)
    return recorder


def _ruff_call(recorder: _RuffRecorder) -> dict:
    assert recorder.calls, (
        "the helper never invoked ruff through subprocess.run/check_output"
    )
    return recorder.calls[-1]


def _joined(violations: list[str]) -> str:
    return "\n".join(violations)


def _real_ruff_available() -> bool:
    found = shutil.which("ruff")
    if not found:
        return False
    proc = subprocess.run(
        [found, "--version"], capture_output=True, text=True, check=False
    )
    return proc.returncode == 0


requires_real_ruff = pytest.mark.skipif(
    not _real_ruff_available(), reason="ruff not installed"
)


def _repo_with_ruff_config(root: Path, select: list[str]) -> Path:
    """A target repo whose ruff config selects exactly ``select``."""
    root.mkdir(parents=True, exist_ok=True)
    quoted = ", ".join(f'"{rule}"' for rule in select)
    (root / "pyproject.toml").write_text(
        f"[tool.ruff.lint]\nselect = [{quoted}]\n"
    )
    (root / "ruff.toml").write_text(f"lint.select = [{quoted}]\n")
    return root


# --------------------------------------------------------------------------- #
# ingest harness
# --------------------------------------------------------------------------- #
def _py_entry(source: str, path: str = "tests/unit/test_fixture.py") -> dict:
    return {"path": path, "source": source}


def _story(key: str, acceptance: list[dict]) -> dict:
    return {"key": key, "summary": f"Story {key}", "acceptance": acceptance}


def _write_plan(plan_dir: Path, name: str, stories: list[dict]) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan = {
        "name": name,
        "repo_root": str(plan_dir),
        "epics": [{"summary": "Epic", "stories": stories}],
    }
    (plan_dir / f"{name}.json").write_text(json.dumps(plan))


def _ingest(plan_name: str) -> dict:
    from pipeline.ingest import _ingest_plan_impl

    return _ingest_plan_impl(plan_name)


# --------------------------------------------------------------------------- #
# 1. The module and its public API
# --------------------------------------------------------------------------- #
def test_module_file_exists() -> None:
    assert MODULE_PATH.is_file(), (
        f"{MODULE_PATH} does not exist: the story requires a NEW standalone "
        "module pipeline/fixture_lint.py"
    )


def test_requirements_dev_pins_ruff_exactly() -> None:
    """A version mismatch silently changes what 'clean' means."""
    text = REQUIREMENTS_DEV.read_text()
    assert f"ruff=={PINNED_RUFF}" in text, (
        f"requirements-dev.txt must pin ruff=={PINNED_RUFF} exactly"
    )


def test_signature(lint) -> None:
    sig = inspect.signature(lint)
    params = list(sig.parameters.values())
    assert [p.name for p in params] == ["source", "repo_root"], (
        f"expected lint_acceptance_source(source, repo_root), got {sig}"
    )
    assert str(params[0].annotation) == "str"
    assert "Path" in str(params[1].annotation)
    assert "list" in str(sig.return_annotation)
    assert "str" in str(sig.return_annotation)


def test_module_is_standalone(fl) -> None:
    """No dependency on pipeline.server / pipeline.ingest (OPSA-7 imports it)."""
    source = MODULE_PATH.read_text()
    for forbidden in (
        "pipeline.server",
        "pipeline.ingest",
        "from .server",
        "from .ingest",
        "import server",
        "import ingest",
    ):
        assert forbidden not in source, (
            f"pipeline/fixture_lint.py references {forbidden!r}; it must stay "
            "standalone so the OPSA-7 patch_acceptance executor can import it"
        )


def test_function_is_defined_in_the_new_module(lint) -> None:
    assert Path(inspect.getsourcefile(lint)).resolve() == MODULE_PATH.resolve()


# --------------------------------------------------------------------------- #
# 2. Clean sources -> []
# --------------------------------------------------------------------------- #
def test_clean_source_returns_empty_list(lint, tmp_path: Path) -> None:
    assert lint(CLEAN_SOURCE, tmp_path) == []


def test_clean_source_returns_empty_list_with_fake_ruff(
    lint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_ruff(monkeypatch, _RuffRecorder(stdout="", returncode=0), tmp_path)
    assert lint(CLEAN_SOURCE, tmp_path) == []


def test_empty_source_is_clean(lint, tmp_path: Path) -> None:
    assert lint("", tmp_path) == []


def test_whitespace_only_source_is_clean(lint, tmp_path: Path) -> None:
    assert lint("   \n\n\t\n", tmp_path) == []


def test_return_value_is_a_list_of_strings(lint, tmp_path: Path) -> None:
    result = lint(CLEAN_SOURCE, tmp_path)
    assert isinstance(result, list)
    assert all(isinstance(item, str) for item in result)


# --------------------------------------------------------------------------- #
# 3. Violations are reported
# --------------------------------------------------------------------------- #
def test_unused_variable_violation_is_reported(lint, tmp_path: Path) -> None:
    """The brief's headline case: an unused-variable (RUF059-shaped) finding."""
    repo = _repo_with_ruff_config(tmp_path / "repo", ["RUF059", "F", "E"])
    violations = lint(RUF059_SOURCE, repo)
    assert violations, "an unused unpacked variable was reported as clean"
    assert "RUF059" in _joined(violations)
    assert "unused_value" in _joined(violations)


def test_unused_variable_violation_with_fake_ruff(
    lint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = _RuffRecorder(
        stdout=f"tests/unit/test_fixture.py:5:5: {RUF059_LINE}\n", returncode=1
    )
    _fake_ruff(monkeypatch, recorder, tmp_path)
    violations = lint(RUF059_SOURCE, tmp_path)
    assert violations
    assert RUF059_LINE in _joined(violations)


def test_violation_lines_are_returned_verbatim(
    lint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stdout = f"tests/unit/test_fixture.py:3:8: {F401_LINE}\n"
    _fake_ruff(monkeypatch, _RuffRecorder(stdout=stdout, returncode=1), tmp_path)
    violations = lint(F401_SOURCE, tmp_path)
    assert violations == [f"tests/unit/test_fixture.py:3:8: {F401_LINE}"]


def test_multiple_violations_are_all_listed(
    lint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stdout = (
        f"tests/unit/test_fixture.py:3:8: {F401_LINE}\n"
        f"tests/unit/test_fixture.py:7:5: {F841_LINE}\n"
        f"tests/unit/test_fixture.py:9:5: {RUF059_LINE}\n"
    )
    _fake_ruff(monkeypatch, _RuffRecorder(stdout=stdout, returncode=1), tmp_path)
    violations = lint(F401_SOURCE, tmp_path)
    joined = _joined(violations)
    assert len(violations) >= 3, f"only {len(violations)} of 3 violations listed"
    for expected in (F401_LINE, F841_LINE, RUF059_LINE):
        assert expected in joined, f"{expected!r} missing from {violations!r}"


def test_unused_import_violation_is_reported(lint, tmp_path: Path) -> None:
    """F401 is in ruff's default rule set, so no config is needed."""
    violations = lint(F401_SOURCE, tmp_path)
    assert violations, "an unused import (F401) was reported as clean"
    assert "F401" in _joined(violations)


def test_syntax_error_source_is_a_violation_not_a_crash(
    lint, tmp_path: Path
) -> None:
    violations = lint("def test_broken(:\n    assert True\n", tmp_path)
    assert violations, "a syntax error (E9) was reported as clean"


# --------------------------------------------------------------------------- #
# 4. SOURCE-ONLY: the fixture is never imported or executed
# --------------------------------------------------------------------------- #
def test_fixture_is_never_imported_or_executed(lint, tmp_path: Path) -> None:
    marker = tmp_path / "EXECUTED"
    hostile = (
        '"""Fixture that would leave a marker if executed."""\n'
        "from pathlib import Path\n\n"
        f"Path({str(marker)!r}).write_text('executed')\n\n\n"
        "def test_ok() -> None:\n"
        "    assert 1 + 1 == 2\n"
    )
    lint(hostile, tmp_path)
    assert not marker.exists(), (
        "the fixture source was imported/executed; linting must be text-only"
    )


def test_source_is_materialized_into_a_temp_file(
    lint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = _fake_ruff(monkeypatch, _RuffRecorder(), tmp_path)
    lint(CLEAN_SOURCE, tmp_path)
    call = _ruff_call(recorder)
    assert call["content"] == CLEAN_SOURCE, (
        "ruff was not pointed at a file containing the fixture source"
    )
    target = Path(call["target"])
    assert target.is_file()
    assert target.suffix == ".py"
    assert str(target.resolve()).startswith(
        str(Path(tempfile.gettempdir()).resolve())
    ), f"the materialized fixture {target} is not under a temp dir"


def test_ruff_is_invoked_as_a_check(lint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    recorder = _fake_ruff(monkeypatch, _RuffRecorder(), tmp_path)
    lint(CLEAN_SOURCE, tmp_path)
    cmd = _ruff_call(recorder)["cmd"]
    assert "check" in cmd, f"ruff was not run as 'ruff check': {cmd}"


def test_fixture_module_is_not_added_to_sys_modules(lint, tmp_path: Path) -> None:
    before = set(sys.modules)
    lint(CLEAN_SOURCE, tmp_path)
    added = {
        name
        for name in set(sys.modules) - before
        if "fixture" in name or name.startswith("test_")
    }
    assert not added, f"linting imported the fixture into sys.modules: {added}"


# --------------------------------------------------------------------------- #
# 5. FAIL CLOSED: ruff cannot run at all
# --------------------------------------------------------------------------- #
def test_missing_ruff_binary_raises(lint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_ruff(
        monkeypatch, _RuffRecorder(missing=True), tmp_path, hide_ruff=True
    )
    with pytest.raises(Exception) as excinfo:
        lint(CLEAN_SOURCE, tmp_path)
    assert "ruff" in str(excinfo.value).lower(), (
        "the error must name the cause (ruff could not be found): "
        f"{excinfo.value!r}"
    )


def test_ruff_crash_raises(lint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_ruff(
        monkeypatch,
        _RuffRecorder(exc=OSError("ruff exploded")),
        tmp_path,
    )
    with pytest.raises(Exception) as excinfo:
        lint(CLEAN_SOURCE, tmp_path)
    assert "ruff exploded" in str(excinfo.value), (
        f"the error must name the cause: {excinfo.value!r}"
    )


def test_ruff_timeout_raises(lint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_ruff(
        monkeypatch,
        _RuffRecorder(exc=subprocess.TimeoutExpired(cmd="ruff check", timeout=30)),
        tmp_path,
    )
    with pytest.raises(Exception) as excinfo:
        lint(CLEAN_SOURCE, tmp_path)
    assert "time" in str(excinfo.value).lower(), (
        f"the error must name the timeout cause: {excinfo.value!r}"
    )


def test_ruff_call_is_bounded_by_a_timeout(
    lint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = _fake_ruff(monkeypatch, _RuffRecorder(), tmp_path)
    lint(CLEAN_SOURCE, tmp_path)
    kwargs = _ruff_call(recorder)["kwargs"]
    assert "timeout" in kwargs, "the ruff subprocess call is not bounded by a timeout"
    assert isinstance(kwargs["timeout"], (int, float))
    assert kwargs["timeout"] > 0


def test_nonzero_exit_without_output_raises(
    lint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A crash must not be mistaken for 'clean'."""
    _fake_ruff(
        monkeypatch,
        _RuffRecorder(stdout="", stderr="ruff: internal error", returncode=2),
        tmp_path,
    )
    with pytest.raises(Exception) as excinfo:
        lint(CLEAN_SOURCE, tmp_path)
    assert str(excinfo.value).strip(), "the error message must not be empty"


def test_nonzero_exit_with_output_returns_violations(
    lint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ruff exits 1 when it finds violations - that is not a tool failure."""
    stdout = f"tests/unit/test_fixture.py:3:8: {F401_LINE}\n"
    _fake_ruff(monkeypatch, _RuffRecorder(stdout=stdout, returncode=1), tmp_path)
    assert lint(F401_SOURCE, tmp_path) == [f"tests/unit/test_fixture.py:3:8: {F401_LINE}"]


def test_ruff_version_mismatch_raises(
    lint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A different ruff silently changes what 'clean' means."""
    _fake_ruff(monkeypatch, _RuffRecorder(version="0.4.0"), tmp_path)
    with pytest.raises(Exception) as excinfo:
        lint(CLEAN_SOURCE, tmp_path)
    message = str(excinfo.value)
    assert "0.4.0" in message or PINNED_RUFF in message, (
        f"the error must name the version mismatch: {message!r}"
    )


# --------------------------------------------------------------------------- #
# 6. Temp files are cleaned up
# --------------------------------------------------------------------------- #
def _materialized_target(lint, monkeypatch, tmp_path: Path, source: str) -> Path:
    recorder = _fake_ruff(monkeypatch, _RuffRecorder(), tmp_path)
    lint(source, tmp_path)
    return Path(_ruff_call(recorder)["target"])


def test_temp_file_removed_after_clean_lint(
    lint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = _materialized_target(lint, monkeypatch, tmp_path, CLEAN_SOURCE)
    assert not target.exists(), f"temp fixture {target} was left behind"


def test_temp_file_removed_after_violation(
    lint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = _fake_ruff(
        monkeypatch,
        _RuffRecorder(stdout=f"x.py:1:1: {F401_LINE}\n", returncode=1),
        tmp_path,
    )
    lint(F401_SOURCE, tmp_path)
    target = Path(_ruff_call(recorder)["target"])
    assert not target.exists(), f"temp fixture {target} was left behind"


def test_temp_file_removed_after_tool_failure(
    lint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = _fake_ruff(
        monkeypatch, _RuffRecorder(exc=OSError("boom")), tmp_path
    )
    with contextlib.suppress(Exception):
        lint(CLEAN_SOURCE, tmp_path)
    target = Path(_ruff_call(recorder)["target"])
    assert not target.exists(), f"temp fixture {target} was left behind"


def test_temp_directory_removed_after_lint(
    lint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = _materialized_target(lint, monkeypatch, tmp_path, CLEAN_SOURCE)
    system_temp = Path(tempfile.gettempdir()).resolve()
    if target.parent.resolve() != system_temp:
        assert not target.parent.exists(), (
            f"the helper's temp dir {target.parent} was left behind"
        )


# --------------------------------------------------------------------------- #
# 7. The repo's ruff config is honoured; ruff comes from the pipeline's env
# --------------------------------------------------------------------------- #
def _config_is_visible_to_ruff(call: dict, repo: Path) -> bool:
    cmd = call["cmd"]
    if "--config" in cmd:
        idx = cmd.index("--config")
        try:
            config = Path(cmd[idx + 1]).resolve()
        except IndexError:
            return False
        return repo.resolve() in config.parents or config.parent == repo.resolve()
    cwd = call["kwargs"].get("cwd")
    return cwd is not None and Path(cwd).resolve() == repo.resolve()


def test_repo_ruff_config_is_honoured(
    lint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A repo selecting only E501 must not report RUF059."""
    repo = _repo_with_ruff_config(tmp_path / "repo", ["E501"])
    recorder = _fake_ruff(monkeypatch, _RuffRecorder(), tmp_path)
    assert lint(RUF059_SOURCE, repo) == []
    assert _config_is_visible_to_ruff(_ruff_call(recorder), repo), (
        "ruff was not given the repo's config (neither cwd nor --config)"
    )


def test_repo_ruff_config_selecting_ruf059_reports_it(
    lint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repo_with_ruff_config(tmp_path / "repo", ["RUF059"])
    stdout = f"tests/unit/test_fixture.py:5:5: {RUF059_LINE}\n"
    _fake_ruff(monkeypatch, _RuffRecorder(stdout=stdout, returncode=1), tmp_path)
    assert lint(RUF059_SOURCE, repo) == [f"tests/unit/test_fixture.py:5:5: {RUF059_LINE}"]


def test_target_repo_venv_ruff_is_not_used(
    lint, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """repo_root is the TARGET project; its pinned ruff must not gate ingest."""
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = tmp_path / "venv_ruff_ran"
    _write_script(
        repo / ".venv" / "bin" / "ruff",
        f"#!/bin/sh\ntouch {marker}\nexit 1\n",
    )
    fakebin = tmp_path / "fakebin"
    _write_script(fakebin / "ruff", "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv(
        "PATH", f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}"
    )
    assert lint(CLEAN_SOURCE, repo) == []
    assert not marker.exists(), (
        "the target repo's .venv/bin/ruff was executed; ruff must come from "
        "the pipeline's own environment"
    )


# --------------------------------------------------------------------------- #
# 8. Ingest wiring: a lint failure REJECTS the ingest (fail closed)
# --------------------------------------------------------------------------- #
BAD_PATH = "tests/unit/test_bad_fixture.py"


def test_ingest_accepts_clean_py_fixture(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_ruff(monkeypatch, _RuffRecorder(), tmp_path)
    _write_plan(plan_dir, "clean", [_story("S1", [_py_entry(CLEAN_SOURCE)])])
    result = _ingest("clean")
    assert result["ok"] is True, result


def test_ingest_rejects_fixture_with_unused_variable(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stdout = f"{BAD_PATH}:5:5: {RUF059_LINE}\n"
    _fake_ruff(monkeypatch, _RuffRecorder(stdout=stdout, returncode=1), tmp_path)
    _write_plan(
        plan_dir, "bad", [_story("S1", [_py_entry(RUF059_SOURCE, BAD_PATH)])]
    )
    result = _ingest("bad")
    assert result["ok"] is False, "a lint-violating fixture was accepted"
    error = result["error"]
    assert BAD_PATH in error, f"the error must name the entry path: {error!r}"
    assert RUF059_LINE in error, f"the error must quote the violation: {error!r}"


def test_ingest_rejects_fixture_with_unused_import(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stdout = f"{BAD_PATH}:3:8: {F401_LINE}\n"
    _fake_ruff(monkeypatch, _RuffRecorder(stdout=stdout, returncode=1), tmp_path)
    _write_plan(
        plan_dir, "f401", [_story("S1", [_py_entry(F401_SOURCE, BAD_PATH)])]
    )
    result = _ingest("f401")
    assert result["ok"] is False
    assert F401_LINE in result["error"]


def test_ingest_rejects_when_ruff_is_missing(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fail closed: a gate that fails open is advisory."""
    _fake_ruff(
        monkeypatch, _RuffRecorder(missing=True), tmp_path, hide_ruff=True
    )
    _write_plan(plan_dir, "noruff", [_story("S1", [_py_entry(CLEAN_SOURCE)])])
    result = _ingest("noruff")
    assert result["ok"] is False, (
        "ingest accepted a plan while the lint gate could not run at all"
    )
    assert "ruff" in result["error"].lower(), (
        f"the error must name the cause: {result['error']!r}"
    )


def test_ingest_rejects_when_ruff_crashes(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_ruff(monkeypatch, _RuffRecorder(exc=OSError("ruff exploded")), tmp_path)
    _write_plan(plan_dir, "crash", [_story("S1", [_py_entry(CLEAN_SOURCE)])])
    result = _ingest("crash")
    assert result["ok"] is False
    assert "ruff exploded" in result["error"]


def test_ingest_rejects_when_ruff_times_out(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_ruff(
        monkeypatch,
        _RuffRecorder(exc=subprocess.TimeoutExpired(cmd="ruff check", timeout=30)),
        tmp_path,
    )
    _write_plan(plan_dir, "timeout", [_story("S1", [_py_entry(CLEAN_SOURCE)])])
    result = _ingest("timeout")
    assert result["ok"] is False
    assert "time" in result["error"].lower()


def test_ingest_error_lists_every_violation(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stdout = (
        f"{BAD_PATH}:3:8: {F401_LINE}\n"
        f"{BAD_PATH}:7:5: {F841_LINE}\n"
        f"{BAD_PATH}:9:5: {RUF059_LINE}\n"
    )
    _fake_ruff(monkeypatch, _RuffRecorder(stdout=stdout, returncode=1), tmp_path)
    _write_plan(
        plan_dir, "multi", [_story("S1", [_py_entry(F401_SOURCE, BAD_PATH)])]
    )
    result = _ingest("multi")
    assert result["ok"] is False
    for expected in (F401_LINE, F841_LINE, RUF059_LINE):
        assert expected in result["error"], (
            f"{expected!r} missing from the rejection: {result['error']!r}"
        )


def test_ingest_skips_non_py_acceptance_paths(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only .py entries are linted; a .md entry is skipped."""
    recorder = _fake_ruff(
        monkeypatch,
        _RuffRecorder(stdout=f"{BAD_PATH}:1:1: {F401_LINE}\n", returncode=1),
        tmp_path,
    )
    _write_plan(
        plan_dir,
        "nonpy",
        [
            _story(
                "S1",
                [
                    {"path": "docs/acceptance.md", "source": "not python at all"},
                    {"path": "tests/acceptance.txt", "source": "import pytest"},
                ],
            )
        ],
    )
    result = _ingest("nonpy")
    assert result["ok"] is True, result
    assert not recorder.calls, "ruff was invoked for a non-.py acceptance entry"


def test_ingest_accepts_story_without_acceptance_entries(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_ruff(monkeypatch, _RuffRecorder(), tmp_path)
    _write_plan(plan_dir, "empty", [_story("S1", [])])
    assert _ingest("empty")["ok"] is True


def test_ingest_accepts_empty_acceptance_source(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_ruff(monkeypatch, _RuffRecorder(), tmp_path)
    _write_plan(plan_dir, "blank", [_story("S1", [_py_entry("")])])
    assert _ingest("blank")["ok"] is True


# --------------------------------------------------------------------------- #
# 9. The two advisory warnings are a different failure class: still green
# --------------------------------------------------------------------------- #
def test_advisory_warning_helpers_still_exist() -> None:
    from pipeline import server as p

    assert callable(getattr(p, "_isolation_only_acceptance_warning", None)), (
        "_isolation_only_acceptance_warning must stay available"
    )
    assert callable(getattr(p, "_platform_locked_fixture_warning", None)), (
        "_platform_locked_fixture_warning must stay available"
    )


def test_isolation_only_fixture_still_ingests(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The advisory warning stays non-blocking for a lint-clean fixture."""
    _fake_ruff(monkeypatch, _RuffRecorder(), tmp_path)
    source = (
        '"""Isolation-only acceptance fixture."""\n\n\n'
        "def _no_tool_nudge(value: int) -> int:\n"
        "    return value\n\n\n"
        "def test_x() -> None:\n"
        "    assert _no_tool_nudge(0) == 0\n"
    )
    _write_plan(
        plan_dir,
        "iso",
        [_story("S1", [_py_entry(source, "tests/test_nudge.py")])],
    )
    result = _ingest("iso")
    assert result["ok"] is True, result


# --------------------------------------------------------------------------- #
# 10. The fixture sources embedded in the EXISTING ingest tests must pass the
#     new check (the brief: "run the full ingest unit tests and verify").
# --------------------------------------------------------------------------- #
INGEST_TEST_FILES = (
    "tests/unit/test_ingest_plan_risk_lock.py",
    "tests/unit/test_pipeline_mcp_server_ingest_and_review_gate.py",
    "tests/unit/test_pipeline_mcp_server_decisions_and_dispatch.py",
)

DECISIONS_TEST = "tests/unit/test_pipeline_mcp_server_decisions_and_dispatch.py"


def _embedded_py_acceptance_sources(path: Path) -> list[tuple[int, str]]:
    """Every ``{"path": "...py", "source": "..."}`` literal in a test file."""
    tree = ast.parse(path.read_text())
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        pairs: dict[str, ast.expr] = {}
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                pairs[key.value] = value
        src, pth = pairs.get("source"), pairs.get("path")
        if not isinstance(src, ast.Constant) or not isinstance(src.value, str):
            continue
        if not isinstance(pth, ast.Constant) or not isinstance(pth.value, str):
            continue
        if not pth.value.endswith(".py"):
            continue
        found.append((node.lineno, src.value))
    return found


def test_embedded_source_extraction_finds_the_known_fixtures() -> None:
    """Guards the extraction above so the checks below cannot go vacuous."""
    found = _embedded_py_acceptance_sources(REPO_ROOT / DECISIONS_TEST)
    assert len(found) >= 2, (
        f"expected the two embedded .py acceptance sources in {DECISIONS_TEST}, "
        f"found {found!r}"
    )


@pytest.mark.parametrize("rel", INGEST_TEST_FILES)
def test_embedded_fixture_sources_in_existing_ingest_tests_are_lint_clean(
    rel: str, lint, tmp_path: Path
) -> None:
    """A fixture source embedded in an existing ingest test must lint clean."""
    path = REPO_ROOT / rel
    assert path.is_file(), f"{rel} is missing"
    for lineno, source in _embedded_py_acceptance_sources(path):
        violations = lint(source, tmp_path)
        assert violations == [], (
            f"{rel}:{lineno} embeds an acceptance fixture source that fails the "
            f"new lint check: {violations}"
        )


def test_existing_ingest_unit_tests_still_pass() -> None:
    """The brief: run the full ingest unit tests and verify they stay green."""
    files = [str(REPO_ROOT / rel) for rel in INGEST_TEST_FILES]
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *files,
            "-q",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            "--no-header",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert proc.returncode == 0, (
        "the existing ingest unit tests no longer pass:\n"
        + proc.stdout[-4000:]
        + proc.stderr[-2000:]
    )


def test_superseded_delete_the_module_contract_is_gone() -> None:
    """The committed test pinned the opposite resolution; it must not survive.

    ``tests/unit/test_ingest_fixture_lint.py`` asserts that
    ``pipeline/fixture_lint.py`` must NOT exist and that the gate fails OPEN
    when ruff is absent.  Both contradict this story, so that assertion has to
    go (the file may be deleted or rewritten).
    """
    stale = REPO_ROOT / "tests" / "unit" / "test_ingest_fixture_lint.py"
    if not stale.exists():
        return
    text = stale.read_text()
    assert "test_parallel_fixture_lint_module_is_deleted" not in text, (
        f"{stale} still asserts that pipeline/fixture_lint.py must not exist, "
        "which contradicts this story's requirement to add it"
    )
