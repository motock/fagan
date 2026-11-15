"""Startup refusal for retired tenant-scoped environment variables (ADR 0015).

The eight legacy ``PIPELINE_*`` names that the settings registry replaces are
refused at process start when set to any value, including the old defaults
and an empty string. The refusal names each variable and its replacement key,
never a value, and a failure inside the check refuses start too (fail closed).

Both launch paths are covered: the MCP server (``app/pipeline_mcp_server.py``)
and the scheduler daemon (``python -m pipeline.scheduler_daemon``). The wiring
tests run the real entry points in a subprocess, so a refusal is observed as a
process that exits non-zero before it serves anything.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline import settings_env_guard as guard
from pipeline.settings_registry import REGISTRY

REPO_ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "SENTINEL-do-not-echo-7f3a"

RETIRED = {
    setting.legacy_env: setting.name
    for setting in REGISTRY.values()
    if setting.legacy_env
}

EXPECTED_RETIRED = {
    "PIPELINE_MERGE_CI_GATE": "merge_ci_gate",
    "PIPELINE_MERGE_BUILD_GATE": "merge_build_gate",
    "PIPELINE_TEST_SCOPE": "test_scope",
    "PIPELINE_MERGE_CI_TIMEOUT": "merge_ci_timeout_s",
    "PIPELINE_MERGE_READINESS_TIMEOUT": "merge_readiness_timeout_s",
    "PIPELINE_REWORK_ON_CI_FAIL": "rework_on_ci_fail",
    "PIPELINE_REVERIFY_FULL_SUITE": "reverify_full_suite",
    "PIPELINE_GRADE_XDIST_WORKERS": "grade_xdist_workers",
}

OLD_DEFAULTS = {
    "PIPELINE_MERGE_CI_GATE": "1",
    "PIPELINE_MERGE_BUILD_GATE": "1",
    "PIPELINE_TEST_SCOPE": "full",
    "PIPELINE_MERGE_CI_TIMEOUT": "900",
    "PIPELINE_MERGE_READINESS_TIMEOUT": "180",
    "PIPELINE_REWORK_ON_CI_FAIL": "0",
    "PIPELINE_REVERIFY_FULL_SUITE": "0",
    "PIPELINE_GRADE_XDIST_WORKERS": "1",
}


def _line(legacy: str, key: str) -> str:
    return (
        f"{legacy} is retired; set {key} with set_setting "
        "(or run python -m pipeline.settings_seed first). See ADR 0015."
    )


def _env_without_retired(**extra: str) -> dict[str, str]:
    env = {name: value for name, value in os.environ.items() if name not in RETIRED}
    env.update(extra)
    return env


def test_registry_retires_exactly_the_eight_legacy_names():
    assert RETIRED == EXPECTED_RETIRED


@pytest.mark.parametrize("legacy,key", sorted(EXPECTED_RETIRED.items()))
def test_each_retired_variable_alone_is_reported_with_its_key(legacy, key):
    assert guard.retired_env_present({legacy: SENTINEL}) == [
        {"name": legacy, "replacement": key}
    ]


@pytest.mark.parametrize("legacy", sorted(OLD_DEFAULTS))
def test_retired_variable_at_its_old_default_still_refuses(legacy):
    assert [
        f["name"] for f in guard.retired_env_present({legacy: OLD_DEFAULTS[legacy]})
    ] == [legacy]


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_retired_variable_still_refuses(value):
    assert [
        f["name"] for f in guard.retired_env_present({"PIPELINE_TEST_SCOPE": value})
    ] == ["PIPELINE_TEST_SCOPE"]


def test_setting_without_a_legacy_name_is_never_reported_for_an_empty_name():
    # Keys with no retired name store "" as legacy_env. An empty-named variable
    # must not be reported as retiring them.
    assert guard.retired_env_present({"": SENTINEL}) == []


def test_no_retired_variable_reports_nothing():
    environ = {"PATH": "/usr/bin", "PIPELINE_LOCAL_NUM_CTX": "16384"}
    assert guard.retired_env_present(environ) == []


def test_no_retired_variable_lets_start(capsys):
    assert guard.refuse_retired_env({"PATH": "/usr/bin"}) is None
    assert capsys.readouterr().err == ""


def test_refusal_prints_one_line_per_variable_without_values(capsys):
    environ = {legacy: SENTINEL for legacy in EXPECTED_RETIRED}

    with pytest.raises(SystemExit) as exc:
        guard.refuse_retired_env(environ)

    err = capsys.readouterr().err
    assert exc.value.code == 1
    assert SENTINEL not in err
    lines = err.splitlines()
    assert sorted(lines) == sorted(_line(l, k) for l, k in EXPECTED_RETIRED.items())


def test_refusal_line_matches_the_documented_message(capsys):
    with pytest.raises(SystemExit):
        guard.refuse_retired_env({"PIPELINE_MERGE_CI_TIMEOUT": "600"})

    expected = (
        "PIPELINE_MERGE_CI_TIMEOUT is retired; set merge_ci_timeout_s with set_setting "
        "(or run python -m pipeline.settings_seed first). See ADR 0015."
    )
    assert capsys.readouterr().err.splitlines() == [expected]


def test_default_environment_is_read_when_no_mapping_is_given(monkeypatch, capsys):
    for legacy in EXPECTED_RETIRED:
        monkeypatch.delenv(legacy, raising=False)
    monkeypatch.setenv("PIPELINE_GRADE_XDIST_WORKERS", SENTINEL)

    with pytest.raises(SystemExit):
        guard.refuse_retired_env()

    assert SENTINEL not in capsys.readouterr().err


def test_error_inside_the_check_refuses_start(monkeypatch, capsys):
    def broken(environ):
        raise RuntimeError(f"cannot read environment {SENTINEL}")

    monkeypatch.setattr(guard, "retired_env_present", broken)

    with pytest.raises(SystemExit) as exc:
        guard.refuse_retired_env({"PATH": "/usr/bin"})

    err = capsys.readouterr().err
    assert exc.value.code == 1
    assert "RuntimeError" in err
    assert SENTINEL not in err


def test_scheduler_daemon_refuses_to_start_with_a_retired_variable():
    result = subprocess.run(
        [sys.executable, "-m", "pipeline.scheduler_daemon"],
        cwd=REPO_ROOT,
        env=_env_without_retired(PIPELINE_MERGE_CI_GATE=SENTINEL),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 1
    assert _line("PIPELINE_MERGE_CI_GATE", "merge_ci_gate") in result.stderr
    assert SENTINEL not in result.stderr
    assert SENTINEL not in result.stdout


def test_mcp_server_refuses_to_start_with_a_retired_variable():
    result = subprocess.run(
        [sys.executable, "app/pipeline_mcp_server.py"],
        cwd=REPO_ROOT,
        env=_env_without_retired(PIPELINE_TEST_SCOPE=SENTINEL),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 1
    assert _line("PIPELINE_TEST_SCOPE", "test_scope") in result.stderr
    assert SENTINEL not in result.stderr
    assert SENTINEL not in result.stdout
