"""TDD suite for scripts/install_checks.py (written BEFORE the implementation).

``install_checks`` is the pre-venv doctor for the repo: it reports which tools
the pipeline needs (python >= 3.10, git, gh, claude - required; ollama, docker
- optional) and never fails the process; install.sh (a later story) decides
what to do with the report.

Host-independence rules enforced here (brief: "Do NOT run the real
shutil.which in tests - that would assert today's host, which is forbidden"):

* every ``collect_checks`` call injects a stub ``which``; the real
  ``shutil.which`` is never invoked in-process (it is only referenced to
  assert the signature default);
* every ``py_version`` is a synthetic ``(major, minor)`` tuple, except the
  default-wiring test, which asserts relative to ``sys.version_info`` instead
  of pinning a host version;
* ``main`` is tested in-process against a stubbed ``collect_checks``, and
  end-to-end only via subprocesses whose ``PATH`` is emptied, so every tool is
  deterministically absent regardless of the host;
* hints are asserted free of host paths, usernames and env values.

This file is RED (collection-time ImportError below) until
``scripts/install_checks.py`` exists - that is the intended TDD state.
"""

import ast
import getpass
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "install_checks.py"

if not _SCRIPT.exists():
    raise ImportError(
        f"TDD red: {_SCRIPT} does not exist yet. Implement "
        "scripts/install_checks.py with exactly collect_checks() and main() "
        "per the contract in this test module."
    )

_spec = importlib.util.spec_from_file_location("install_checks", str(_SCRIPT))
ic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ic)

_REQUIRED_TOOLS = ("python", "git", "gh", "claude")
_OPTIONAL_TOOLS = ("ollama", "docker")
_ALL_TOOLS = _REQUIRED_TOOLS + _OPTIONAL_TOOLS
# "python3" is stubbed too because the python check may legitimately consult
# either binary name; the stub must answer both so the version logic alone
# decides the python check's status in the happy-path tests.
_ALL_PRESENT = set(_ALL_TOOLS) | {"python3"}
_PYTHON_MIN = (3, 10)
_EXACT_PYTHON_MISSING_HINT = "Install Python 3.10+ (the mcp SDK needs it)"
_CHECK_KEYS = {"name", "required", "status", "detail", "hint"}
_STUB_PATH_PREFIX = "/fake/toolchain/bin/"


def _which_stub(present):
    """Return a shutil.which stand-in: fake path for names in ``present``."""

    def _which(name, *args, **kwargs):
        if name in present:
            return _STUB_PATH_PREFIX + name
        return None

    return _which


def _collect(present=_ALL_PRESENT, py_version=_PYTHON_MIN):
    """collect_checks with the real which() stubbed out, never the host's."""
    return ic.collect_checks(which=_which_stub(set(present)), py_version=py_version)


def _by_name(checks):
    return {check["name"]: check for check in checks}


def _tree():
    return ast.parse(_SCRIPT.read_text(encoding="utf-8"))


def _patch_collect(monkeypatch, checks):
    """Route main() through a fixed collect_checks result for determinism."""
    monkeypatch.setattr(
        ic, "collect_checks", lambda *args, **kwargs: [dict(c) for c in checks]
    )


# A fixed, mixed-status result used to pin main()'s rendering contract without
# touching the host. Hints deliberately never mention another check's name so
# the one-line-per-check assertions stay unambiguous.
_MIXED_CHECKS = [
    {
        "name": "python",
        "required": True,
        "status": "ok",
        "detail": "3.11.2",
        "hint": "Install Python 3.10+ (the mcp SDK needs it)",
    },
    {
        "name": "git",
        "required": True,
        "status": "ok",
        "detail": "version 2.39",
        "hint": "Install the git VCS from git-scm.com",
    },
    {
        "name": "gh",
        "required": True,
        "status": "missing",
        "detail": "not found",
        "hint": "Install the GitHub CLI from cli.github.com ; then run: gh auth login",
    },
    {
        "name": "claude",
        "required": True,
        "status": "ok",
        "detail": "version 1.0",
        "hint": "Install the Claude Code CLI",
    },
    {
        "name": "ollama",
        "required": False,
        "status": "missing",
        "detail": "not found",
        "hint": "Optional: install the ollama runtime for local models",
    },
    {
        "name": "docker",
        "required": False,
        "status": "ok",
        "detail": "version 24",
        "hint": "Optional: install Docker for container runs",
    },
]


# ---------------------------------------------------------------------------
# collect_checks: shape and injectability
# ---------------------------------------------------------------------------


def test_collect_checks_signature_is_injectable():
    sig = inspect.signature(ic.collect_checks)
    assert set(sig.parameters) == {"which", "py_version"}
    assert sig.parameters["which"].default is shutil.which
    assert sig.parameters["py_version"].default is None


def test_collect_returns_a_list_with_one_entry_per_specified_check():
    checks = _collect()
    assert isinstance(checks, list)
    assert len(checks) == len(_ALL_TOOLS)
    names = [check["name"] for check in checks]
    assert sorted(names) == sorted(_ALL_TOOLS)
    assert len(set(names)) == len(_ALL_TOOLS), "duplicate check names"


def test_every_check_has_exactly_the_documented_shape():
    for check in _collect() + _collect(present=set()):
        assert set(check) == _CHECK_KEYS, check["name"]
        assert isinstance(check["name"], str) and check["name"]
        assert isinstance(check["required"], bool), check["name"]
        assert check["status"] in ("ok", "missing"), check["name"]
        assert isinstance(check["detail"], str), check["name"]
        assert isinstance(check["hint"], str) and check["hint"].strip(), check["name"]


def test_checks_are_json_serializable():
    json.dumps(_collect())


def test_required_and_optional_flags_match_the_spec():
    by_name = {check["name"]: check for check in _collect()}
    for name in _REQUIRED_TOOLS:
        assert by_name[name]["required"] is True, name
    for name in _OPTIONAL_TOOLS:
        assert by_name[name]["required"] is False, name


# ---------------------------------------------------------------------------
# collect_checks: happy path and required-tool failure
# ---------------------------------------------------------------------------


def test_all_required_present_reports_every_check_ok():
    for check in _collect():
        assert check["status"] == "ok", check


def test_missing_required_tool_is_missing_with_flag_and_actionable_hint():
    by_name = {check["name"]: check for check in _collect(present=_ALL_PRESENT - {"gh"})}
    gh = by_name["gh"]
    assert gh["status"] == "missing"
    assert gh["required"] is True
    assert isinstance(gh["hint"], str) and gh["hint"].strip()
    assert "gh" in gh["hint"].lower(), "hint must name the missing tool"
    for name in ("python", "git", "claude"):
        assert by_name[name]["status"] == "ok", name


def test_every_missing_tool_hint_names_the_tool():
    for check in _collect(present=set()):
        assert check["name"].lower() in check["hint"].lower(), check


# ---------------------------------------------------------------------------
# collect_checks: python version boundaries
# ---------------------------------------------------------------------------


def test_python_at_exact_minimum_3_10_is_ok():
    by_name = {check["name"]: check for check in _collect(py_version=(3, 10))}
    assert by_name["python"]["status"] == "ok"


def test_python_3_9_is_missing_with_the_exact_required_hint():
    py = {check["name"]: check for check in _collect(py_version=(3, 9))}["python"]
    assert py["status"] == "missing"
    assert py["required"] is True
    assert py["hint"] == _EXACT_PYTHON_MISSING_HINT


@pytest.mark.parametrize("py_version", [(3, 11), (3, 19), (4, 0)])
def test_python_versions_above_minimum_are_ok(py_version):
    by_name = {check["name"]: check for check in _collect(py_version=py_version)}
    assert by_name["python"]["status"] == "ok"


@pytest.mark.parametrize("py_version", [(2, 7), (3, 0), (3, 9)])
def test_python_versions_below_minimum_are_missing(py_version):
    by_name = {check["name"]: check for check in _collect(py_version=py_version)}
    assert by_name["python"]["status"] == "missing"


def test_py_version_defaults_to_the_running_interpreter():
    expected = "ok" if sys.version_info[:2] >= _PYTHON_MIN else "missing"
    explicit = {c["name"]: c for c in _collect(py_version=None)}["python"]
    omitted = {
        c["name"]: c for c in ic.collect_checks(which=_which_stub(_ALL_PRESENT))
    }["python"]
    assert explicit["status"] == expected
    assert omitted["status"] == expected


# ---------------------------------------------------------------------------
# collect_checks: optional tools and empty PATH
# ---------------------------------------------------------------------------


def test_optional_tools_absent_are_missing_not_required_and_main_exits_0(monkeypatch):
    checks = _collect(present=_ALL_PRESENT - {"ollama", "docker"})
    by_name = {check["name"]: check for check in checks}
    for name in ("ollama", "docker"):
        assert by_name[name]["status"] == "missing", name
        assert by_name[name]["required"] is False, name
        assert by_name[name]["hint"].strip(), name
    _patch_collect(monkeypatch, checks)
    assert ic.main([]) == 0
    assert ic.main(["--json"]) == 0


def test_empty_path_stubs_every_check_missing_without_raising():
    # Brief: "stub which to always return None -> all missing, no exception".
    # This includes python: an empty PATH means no interpreter binary can be
    # located even with a sufficient version.
    checks = _collect(present=set(), py_version=_PYTHON_MIN)
    assert len(checks) == len(_ALL_TOOLS)
    for check in checks:
        assert check["status"] == "missing", check
        assert check["hint"].strip(), check
    by_name = {check["name"]: check for check in checks}
    for name in _REQUIRED_TOOLS:
        assert by_name[name]["required"] is True, name
    for name in _OPTIONAL_TOOLS:
        assert by_name[name]["required"] is False, name


# ---------------------------------------------------------------------------
# hint portability (no host paths, usernames, or env values)
# ---------------------------------------------------------------------------


def _current_username():
    # getpass.getuser() documents KeyError/OSError/ImportError as its failure
    # modes on hosts without a login identity; catching only those keeps the
    # helper lint-clean (no blind except) while still degrading to None.
    try:
        return getpass.getuser()
    except (KeyError, OSError, ImportError):  # pragma: no cover - exotic hosts
        return None


def _forbidden_hint_literals():
    literals = [_STUB_PATH_PREFIX, str(_REPO_ROOT)]
    for value in (
        os.path.expanduser("~"),
        tempfile.gettempdir(),
        os.getcwd(),
        os.environ.get("HOME", ""),
        os.environ.get("VIRTUAL_ENV", ""),
    ):
        if len(value) >= 6:
            literals.append(value)
    return literals


def test_hints_contain_no_host_paths_usernames_or_env_values():
    hints = [c["hint"] for c in _collect()] + [c["hint"] for c in _collect(present=set())]
    assert hints and all(h.strip() for h in hints)
    for hint in hints:
        assert not hint.startswith("/"), hint
        assert not re.search(r"/(Users|home)/", hint), hint
        for bad in _forbidden_hint_literals():
            assert bad not in hint, (bad, hint)
    username = _current_username()
    if username and len(username) >= 4:
        for hint in hints:
            assert not re.search(rf"\b{re.escape(username)}\b", hint), hint


# ---------------------------------------------------------------------------
# main(): report mode, json mode, argv handling, exit codes
# ---------------------------------------------------------------------------


def test_main_signature_takes_optional_argv():
    sig = inspect.signature(ic.main)
    assert set(sig.parameters) == {"argv"}
    assert sig.parameters["argv"].default is None


def test_main_report_mode_prints_one_line_per_check_and_returns_zero(monkeypatch, capsys):
    _patch_collect(monkeypatch, _MIXED_CHECKS)
    assert ic.main([]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == len(_MIXED_CHECKS)
    for check in _MIXED_CHECKS:
        matching = [
            line
            for line in lines
            if re.search(rf"\b{re.escape(check['name'])}\b", line)
        ]
        assert len(matching) == 1, (check["name"], lines)
        line = matching[0]
        if check["status"] == "ok":
            assert "[ok]" in line, line
        elif check["required"]:
            assert "[MISS]" in line, line
        else:
            assert "[MISS]" in line or "[optional]" in line, line


def test_main_json_mode_prints_the_json_list_and_returns_zero(monkeypatch, capsys):
    _patch_collect(monkeypatch, _MIXED_CHECKS)
    assert ic.main(["--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert isinstance(parsed, list)
    assert parsed == _MIXED_CHECKS


def test_main_with_argv_none_reads_sys_argv(monkeypatch, capsys):
    _patch_collect(monkeypatch, _MIXED_CHECKS)
    monkeypatch.setattr(sys, "argv", ["install_checks.py", "--json"])
    assert ic.main() == 0
    assert json.loads(capsys.readouterr().out) == _MIXED_CHECKS


@pytest.mark.parametrize("argv", [[], ["--json"]])
def test_main_returns_zero_even_when_required_tools_are_missing(monkeypatch, capsys, argv):
    all_missing = [
        {
            "name": name,
            "required": name in _REQUIRED_TOOLS,
            "status": "missing",
            "detail": "not found",
            "hint": f"Install {name} to continue",
        }
        for name in _ALL_TOOLS
    ]
    _patch_collect(monkeypatch, all_missing)
    assert ic.main(argv) == 0
    capsys.readouterr()


# ---------------------------------------------------------------------------
# standalone execution (bare interpreter, no project venv)
# ---------------------------------------------------------------------------


def _bare_env():
    env = dict(os.environ)
    env["PATH"] = ""
    env.pop("VIRTUAL_ENV", None)
    return env


def _run_script(*args):
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        env=_bare_env(),
        cwd=str(_REPO_ROOT),
        timeout=120,
        check=False,
    )


def test_script_runs_standalone_with_empty_path_one_line_per_check():
    proc = _run_script()
    assert proc.returncode == 0, proc.stderr
    assert "Traceback" not in proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) >= len(_ALL_TOOLS)
    for name in _ALL_TOOLS:
        matching = [
            line for line in lines if re.search(rf"\b{re.escape(name)}\b", line)
        ]
        assert len(matching) == 1, (name, lines)
        assert re.search(r"\[[A-Za-z]+\]", matching[0]), matching[0]


def test_script_json_mode_emits_valid_json_list_deterministically_missing():
    proc = _run_script("--json")
    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert isinstance(parsed, list)
    assert len(parsed) == len(_ALL_TOOLS)
    by_name = {check["name"]: check for check in parsed}
    assert sorted(by_name) == sorted(_ALL_TOOLS)
    for check in parsed:
        assert set(check) == _CHECK_KEYS, check
        assert check["status"] in ("ok", "missing"), check
        assert check["hint"].strip(), check
    # PATH is empty in the subprocess, so every tool is absent on any host.
    for check in parsed:
        assert check["status"] == "missing", check
    for name in _REQUIRED_TOOLS:
        assert by_name[name]["required"] is True, name
    for name in _OPTIONAL_TOOLS:
        assert by_name[name]["required"] is False, name


# ---------------------------------------------------------------------------
# module-level constraints from the brief
# ---------------------------------------------------------------------------


def test_module_defines_exactly_the_two_specified_functions():
    top_level = [
        node
        for node in _tree().body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert len(top_level) == 2, [node.name for node in top_level]
    assert all(isinstance(node, ast.FunctionDef) for node in top_level)
    assert {node.name for node in top_level} == {"collect_checks", "main"}


def test_module_imports_are_stdlib_only():
    roots = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert not node.level, "relative import in a standalone script"
            if node.module:
                roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names), roots - set(sys.stdlib_module_names)
    assert "pipeline" not in roots
    assert "app" not in roots


def test_no_strict_mode_flag_is_added():
    literals = [
        node.value
        for node in ast.walk(_tree())
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert "--strict" not in literals
    assert not any(literal.startswith("--strict=") for literal in literals)