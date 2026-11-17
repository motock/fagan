"""Tests for app.namespace_exempt: operator-listed namespace-exempt paths.

The module under test parses ``PIPELINE_TENANT_DATA_EXEMPT_PATHS`` into a
tuple of extension paths that run without a bound namespace, and answers
whether a request path sits under one of them. Only extension paths may be
listed, so core's own data routes can never be exempted.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app import namespace_exempt

VALID = "/api/ext/console/me"

REFUSED = [
    "/api/plans",
    "/api/ext",
    "/api/ext/",
    "/api/ext/a/",
    "/api/ext/../plans",
    "/api/ext/A",
    "/api/ext/.x",
    "/mcp/x",
    "api/ext/a",
    "/api/ext//a",
    "/api/ext/a b",
]


def _module_source() -> str:
    return Path(namespace_exempt.__file__).read_text(encoding="utf-8")


def _code_without_docstring() -> str:
    """The module's code, with its docstring removed."""
    tree = ast.parse(_module_source())
    body = tree.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    tree.body = body
    return ast.unparse(tree)


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------


def test_env_var_name_is_the_documented_one():
    assert namespace_exempt.EXEMPT_PATHS_ENV == "PIPELINE_TENANT_DATA_EXEMPT_PATHS"


def test_max_entries_is_sixteen():
    assert namespace_exempt.MAX_EXEMPT_PATHS == 16


def test_error_type_is_a_value_error():
    assert issubclass(namespace_exempt.ExemptPathsError, ValueError)


# --------------------------------------------------------------------------
# parsing: accepted values
# --------------------------------------------------------------------------


def test_unset_variable_exempts_nothing():
    assert namespace_exempt.load_exempt_paths({}) == ()


@pytest.mark.parametrize("raw", ["", "  "])
def test_blank_value_exempts_nothing(raw):
    assert namespace_exempt.parse_exempt_paths(raw) == ()


def test_single_valid_path():
    assert namespace_exempt.parse_exempt_paths(VALID) == (VALID,)


def test_several_paths_keep_their_order():
    raw = " /api/ext/a/me , /api/ext/b "
    assert namespace_exempt.parse_exempt_paths(raw) == ("/api/ext/a/me", "/api/ext/b")


def test_sixteen_distinct_entries_are_accepted():
    raw = ",".join(f"/api/ext/p{i}" for i in range(namespace_exempt.MAX_EXEMPT_PATHS))
    parsed = namespace_exempt.parse_exempt_paths(raw)
    assert len(parsed) == 16
    assert parsed[0] == "/api/ext/p0"
    assert parsed[-1] == "/api/ext/p15"


# --------------------------------------------------------------------------
# parsing: refused values
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", REFUSED)
def test_refused_paths_raise(raw):
    with pytest.raises(namespace_exempt.ExemptPathsError):
        namespace_exempt.parse_exempt_paths(raw)


@pytest.mark.parametrize("raw", ["/api/ext/a,,/api/ext/b", "/api/ext/a,"])
def test_empty_entry_is_refused(raw):
    with pytest.raises(namespace_exempt.ExemptPathsError) as excinfo:
        namespace_exempt.parse_exempt_paths(raw)
    assert "entry 2 is empty" in str(excinfo.value)


def test_repeated_entry_is_refused():
    with pytest.raises(namespace_exempt.ExemptPathsError) as excinfo:
        namespace_exempt.parse_exempt_paths("/api/ext/a,/api/ext/a")
    assert "entry 2 repeats" in str(excinfo.value)


def test_seventeen_entries_are_refused():
    raw = ",".join(f"/api/ext/p{i}" for i in range(namespace_exempt.MAX_EXEMPT_PATHS + 1))
    with pytest.raises(namespace_exempt.ExemptPathsError) as excinfo:
        namespace_exempt.parse_exempt_paths(raw)
    assert "at most 16" in str(excinfo.value)


@pytest.mark.parametrize(
    "raw",
    [
        ",".join(f"/api/ext/p{i}" for i in range(16)) + ",/api/plans",
        "/api/plans," + ",".join(f"/api/ext/p{i}" for i in range(16)),
    ],
)
def test_entry_count_is_checked_before_the_entries_are_validated(raw):
    with pytest.raises(namespace_exempt.ExemptPathsError) as excinfo:
        namespace_exempt.parse_exempt_paths(raw)
    assert "at most 16" in str(excinfo.value)


# --------------------------------------------------------------------------
# error messages
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["/api/plans", "/api/ext/A", "/api/ext/a,,/api/ext/b", "/api/ext/a,/api/ext/a"],
)
def test_error_message_names_the_variable(raw):
    with pytest.raises(namespace_exempt.ExemptPathsError) as excinfo:
        namespace_exempt.parse_exempt_paths(raw)
    assert str(excinfo.value).startswith(f"{namespace_exempt.EXEMPT_PATHS_ENV}: ")


def test_error_message_never_contains_the_offending_value():
    with pytest.raises(namespace_exempt.ExemptPathsError) as excinfo:
        namespace_exempt.parse_exempt_paths("/api/SECRETVALUE")
    message = str(excinfo.value)
    assert namespace_exempt.EXEMPT_PATHS_ENV in message
    assert "SECRETVALUE" not in message


# --------------------------------------------------------------------------
# loading from an environment mapping
# --------------------------------------------------------------------------


def test_load_reads_the_named_variable():
    environ = {namespace_exempt.EXEMPT_PATHS_ENV: "/api/ext/a/me"}
    assert namespace_exempt.load_exempt_paths(environ) == ("/api/ext/a/me",)


def test_load_ignores_other_variables():
    assert namespace_exempt.load_exempt_paths({"PIPELINE_SOMETHING_ELSE": "/api/plans"}) == ()


def test_load_reports_a_bad_value():
    environ = {namespace_exempt.EXEMPT_PATHS_ENV: "/api/plans"}
    with pytest.raises(namespace_exempt.ExemptPathsError):
        namespace_exempt.load_exempt_paths(environ)


# --------------------------------------------------------------------------
# is_exempt
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/ext/console/me", "/api/ext/console/me/x"])
def test_exempt_paths_match(path):
    assert namespace_exempt.is_exempt(path, (VALID,)) is True


@pytest.mark.parametrize("path", ["/api/ext/console/meow", "/api/ext/console", "/api/plans"])
def test_other_paths_are_not_exempt(path):
    assert namespace_exempt.is_exempt(path, (VALID,)) is False


def test_nothing_is_exempt_without_exempt_paths():
    assert namespace_exempt.is_exempt(VALID, ()) is False


def test_any_listed_path_matches():
    exempt = ("/api/ext/a", "/api/ext/b")
    assert namespace_exempt.is_exempt("/api/ext/b/x", exempt) is True
    assert namespace_exempt.is_exempt("/api/ext/c", exempt) is False


# --------------------------------------------------------------------------
# module shape
# --------------------------------------------------------------------------


def test_module_has_a_docstring_in_triple_double_quotes():
    source = _module_source()
    assert source.lstrip().startswith('"""')
    doc = ast.get_docstring(ast.parse(source))
    assert doc
    assert namespace_exempt.EXEMPT_PATHS_ENV in doc


def test_module_does_not_use_enterprise_vocabulary():
    code = _code_without_docstring().lower()
    for word in ("console", "org", "invitation", "membership"):
        assert not re.search(rf"\b{word}\b", code), f"{word!r} must not appear in the module"


def test_module_does_not_import_from_pipeline():
    tree = ast.parse(_module_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] != "pipeline"
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            assert (node.module or "").split(".")[0] != "pipeline"


def test_module_does_not_log():
    tree = ast.parse(_module_source())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add((node.module or "").split(".")[0])
    assert "logging" not in imported
    assert "logger" not in vars(namespace_exempt)


def test_import_does_not_read_the_environment(monkeypatch):
    reads: list[str] = []

    class _Tracked(dict):
        def get(self, key, default=None):
            reads.append(key)
            return super().get(key, default)

        def __getitem__(self, key):
            reads.append(key)
            return super().__getitem__(key)

        def __contains__(self, key):
            reads.append(key)
            return super().__contains__(key)

    monkeypatch.setattr(os, "environ", _Tracked(os.environ))
    spec = importlib.util.spec_from_file_location(
        "namespace_exempt_import_probe", namespace_exempt.__file__
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert namespace_exempt.EXEMPT_PATHS_ENV not in reads


def test_extension_prefix_is_taken_from_dashboard_mount():
    repo_root = Path(namespace_exempt.__file__).resolve().parents[1]
    probe = "/api/extprobe"
    script = (
        "import app.dashboard_mount as dm\n"
        f"dm.EXT_API_PREFIX = {probe!r}\n"
        "import app.namespace_exempt as m\n"
        f"print(m.parse_exempt_paths({probe + '/x'!r}))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == repr((probe + "/x",))
