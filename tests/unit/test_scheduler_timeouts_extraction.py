"""The scheduler's env-driven timeout resolvers live in scheduler_timeouts.

Grades the extraction out of ``pipeline/scheduler_daemon.py``: the resolvers
are defined in ``pipeline.scheduler_timeouts``, ``scheduler_daemon`` still
exposes the same objects and still calls them, and the five existing test
files that pinned the old location were re-pointed.
"""
import ast
from pathlib import Path

import pytest

from pipeline import scheduler_daemon, scheduler_timeouts

REPO_ROOT = Path(__file__).resolve().parents[2]
DAEMON_PATH = REPO_ROOT / "pipeline" / "scheduler_daemon.py"
TESTS_DIR = REPO_ROOT / "tests" / "unit"

FLOAT_RESOLVERS = [
    ("_drain_join_timeout_seconds", "PIPELINE_DRAIN_JOIN_TIMEOUT_SECONDS", 120.0),
    ("_scan_join_timeout_seconds", "PIPELINE_SCAN_JOIN_TIMEOUT_SECONDS", 900.0),
    ("_reconcile_join_timeout_seconds", "PIPELINE_RECONCILE_JOIN_TIMEOUT_SECONDS", 900.0),
    ("_abandon_worker_grace_seconds", "PIPELINE_ABANDON_WORKER_GRACE_SECONDS", 300.0),
]
RESOLVER_NAMES = [name for name, _env, _default in FLOAT_RESOLVERS] + [
    "_abandon_restart_threshold",
    "_apply_scheduler_role_call_clamp",
]
FLOAT_IDS = [name for name, _env, _default in FLOAT_RESOLVERS]


@pytest.mark.parametrize("name", RESOLVER_NAMES)
def test_scheduler_timeouts_defines_the_resolver(name):
    assert callable(getattr(scheduler_timeouts, name))


@pytest.mark.parametrize("name", RESOLVER_NAMES)
def test_scheduler_daemon_exposes_the_same_resolver_object(name):
    assert getattr(scheduler_daemon, name) is getattr(scheduler_timeouts, name)


@pytest.mark.parametrize("name", RESOLVER_NAMES)
def test_scheduler_daemon_calls_the_resolver(name):
    assert f"{name}()" in DAEMON_PATH.read_text(encoding="utf-8")


def test_scheduler_daemon_no_longer_defines_the_resolvers():
    tree = ast.parse(DAEMON_PATH.read_text(encoding="utf-8"))
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert defined.isdisjoint(RESOLVER_NAMES)


def test_scheduler_daemon_is_under_one_thousand_lines():
    assert len(DAEMON_PATH.read_text(encoding="utf-8").splitlines()) < 1000


@pytest.mark.parametrize(("name", "env", "default"), FLOAT_RESOLVERS, ids=FLOAT_IDS)
def test_float_resolver_returns_default_when_env_unset(monkeypatch, name, env, default):
    monkeypatch.delenv(env, raising=False)
    assert getattr(scheduler_timeouts, name)() == default


@pytest.mark.parametrize(("name", "env", "default"), FLOAT_RESOLVERS, ids=FLOAT_IDS)
def test_float_resolver_honors_a_valid_env_value(monkeypatch, name, env, default):
    monkeypatch.setenv(env, "42")
    assert getattr(scheduler_timeouts, name)() == 42.0


@pytest.mark.parametrize("raw", ["abc", "nan", "inf", "-5", "0"])
@pytest.mark.parametrize(("name", "env", "default"), FLOAT_RESOLVERS, ids=FLOAT_IDS)
def test_float_resolver_falls_back_to_default_on_a_bad_env_value(
    monkeypatch, name, env, default, raw
):
    monkeypatch.setenv(env, raw)
    assert getattr(scheduler_timeouts, name)() == default


def test_abandon_restart_threshold_returns_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("PIPELINE_SCAN_ABANDON_RESTART_THRESHOLD", raising=False)
    assert scheduler_timeouts._abandon_restart_threshold() == 3


def test_abandon_restart_threshold_honors_a_valid_env_value(monkeypatch):
    monkeypatch.setenv("PIPELINE_SCAN_ABANDON_RESTART_THRESHOLD", "5")
    assert scheduler_timeouts._abandon_restart_threshold() == 5


@pytest.mark.parametrize("raw", ["abc", "2.5"])
def test_abandon_restart_threshold_falls_back_to_default_on_a_bad_env_value(
    monkeypatch, raw
):
    monkeypatch.setenv("PIPELINE_SCAN_ABANDON_RESTART_THRESHOLD", raw)
    assert scheduler_timeouts._abandon_restart_threshold() == 3


def test_drain_ceiling_constant_patched_on_scheduler_timeouts_is_what_the_daemon_reads(
    monkeypatch,
):
    monkeypatch.delenv("PIPELINE_DRAIN_JOIN_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(scheduler_timeouts, "_DRAIN_JOIN_TIMEOUT_SECONDS", 0.5)
    assert scheduler_daemon._drain_join_timeout_seconds() == 0.5


@pytest.mark.parametrize(
    "filename",
    [
        "test_scheduler_drain_bounded.py",
        "test_scheduler_drain_overlap.py",
        "test_scheduler_daemon_scan_watchdog.py",
        "test_scheduler_daemon_abandoned_worker_grace.py",
        "test_scheduler_role_call_clamp.py",
    ],
)
def test_repointed_test_file_references_scheduler_timeouts(filename):
    assert "scheduler_timeouts" in (TESTS_DIR / filename).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "filename", ["test_scheduler_drain_bounded.py", "test_scheduler_drain_overlap.py"]
)
def test_drain_tests_no_longer_patch_the_ceiling_on_scheduler_daemon(filename):
    source = (TESTS_DIR / filename).read_text(encoding="utf-8")
    assert 'setattr(mod, "_DRAIN_JOIN_TIMEOUT_SECONDS"' not in source
