"""Tests for the SCHEDULER_REVISION preflight check (TDD: written first).

The scheduler daemon records ``config.checkout_sha`` /
``config.checkout_behind_origin`` in ``<plan_dir>/.scheduler_health.json``
(prerequisite story MFR-04). ``_check_scheduler_revision`` reads those keys so
a scheduler that is executing pre-merge modules can no longer look exactly
like a current one.

Repo rule applied throughout: *test the resolution logic, not today's
configured values*. Every test injects ``which`` and ``registry_loader``, and
the fingerprint is written into ``tmp_path``. The check is located by NAME in
the returned list (never by index, never by pinning the total), so sibling
stories that add further checks cannot break this file.

Until pipeline/preflight.py grows ``_check_scheduler_revision``, this module
fails at collection/attribute lookup - the expected RED state for this story.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from pipeline import preflight

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT_PATH = REPO_ROOT / "pipeline" / "preflight.py"

# The synthetic checkout revision the daemon would record. Never a real sha.
_SHA = "0847572deadbeef"

_HEALTH_NAME = ".scheduler_health.json"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _ok_which(name):
    """`which` stub: report every CLI as installed (sys.executable exists)."""
    return sys.executable


def _config(plan_dir, **overrides):
    """A well-formed fingerprint config, with per-test overrides applied."""
    config = {
        "plan_dir": str(plan_dir),
        "worktree_root": str(plan_dir),
        "autonomy": "full",
        "dispatch_backend": "claude",
        "checkout_sha": _SHA,
        "checkout_behind_origin": 0,
    }
    config.update(overrides)
    return config


def _health_path(plan_dir):
    return Path(plan_dir) / _HEALTH_NAME


def _write_health(plan_dir, config):
    """Write the daemon's health file; return its path."""
    path = _health_path(plan_dir)
    path.write_text(
        json.dumps({"alive": True, "config": config}), encoding="utf-8"
    )
    return path


def _write_raw_health(plan_dir, raw_text):
    path = _health_path(plan_dir)
    path.write_text(raw_text, encoding="utf-8")
    return path


def _empty_registry():
    """`registry_loader` stub: an empty but valid registry mapping."""
    return {}


def _results(plan_dir, which=None):
    """Run preflight with injected doubles and return every result dict."""
    return preflight.run_preflight(
        plan_dir=plan_dir,
        which=_ok_which if which is None else which,
        registry_loader=_empty_registry,
    )


def _revision_result(plan_dir, which=None):
    """Return the single SCHEDULER_REVISION result (asserting exactly one)."""
    results = _results(plan_dir, which=which)
    matches = [r for r in results if r.get("name") == "SCHEDULER_REVISION"]
    assert len(matches) == 1, (
        "expected exactly one SCHEDULER_REVISION result, got "
        f"{[r.get('name') for r in results]}"
    )
    return matches[0]


# --------------------------------------------------------------------------- #
# 1. Happy path: the daemon runs its checkout's revision.
# --------------------------------------------------------------------------- #
def test_current_checkout_is_ok(tmp_path):
    _write_health(tmp_path, _config(tmp_path, checkout_behind_origin=0))

    result = _revision_result(tmp_path)

    assert result["status"] == "ok"
    assert _SHA in result["message"]


# --------------------------------------------------------------------------- #
# 2. Behind upstream: a warn that names the count and the remedy.
# --------------------------------------------------------------------------- #
def test_being_behind_upstream_is_a_warn_naming_the_count(tmp_path):
    _write_health(tmp_path, _config(tmp_path, checkout_behind_origin=6))

    result = _revision_result(tmp_path)

    assert result["status"] == "warn"
    assert "6" in result["message"]
    assert "restart" in result["message"]


# --------------------------------------------------------------------------- #
# 3. A daemon that predates the revision keys is executing old code.
# --------------------------------------------------------------------------- #
def test_a_fingerprint_without_the_revision_keys_is_a_warn(tmp_path):
    config = _config(tmp_path)
    config.pop("checkout_sha")
    config.pop("checkout_behind_origin")
    _write_health(tmp_path, config)

    result = _revision_result(tmp_path)

    assert result["status"] == "warn"
    assert "does not report" in result["message"]


# --------------------------------------------------------------------------- #
# 4. No fingerprint at all is a normal state, not a problem.
# --------------------------------------------------------------------------- #
def test_no_fingerprint_is_ok(tmp_path):
    assert not _health_path(tmp_path).exists()

    result = _revision_result(tmp_path)

    assert result["status"] == "ok"
    assert "no scheduler fingerprint" in result["message"]


# --------------------------------------------------------------------------- #
# 5. git could not measure the distance: report the sha, do not warn.
# --------------------------------------------------------------------------- #
def test_unmeasurable_distance_is_ok_when_the_sha_is_reported(tmp_path):
    _write_health(tmp_path, _config(tmp_path, checkout_behind_origin=None))

    result = _revision_result(tmp_path)

    assert result["status"] == "ok"
    assert _SHA in result["message"]


# --------------------------------------------------------------------------- #
# 6. Boundary: exactly zero is ok, one is already a warn.
# --------------------------------------------------------------------------- #
def test_exactly_zero_is_ok_and_one_is_a_warn(tmp_path):
    _write_health(tmp_path, _config(tmp_path, checkout_behind_origin=0))
    assert _revision_result(tmp_path)["status"] == "ok"

    _write_health(tmp_path, _config(tmp_path, checkout_behind_origin=1))
    one_behind = _revision_result(tmp_path)
    assert one_behind["status"] == "warn"
    assert "1" in one_behind["message"]


# --------------------------------------------------------------------------- #
# 7. Malformed fingerprints warn, never raise, and never leak the payload.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw_text",
    [
        "{",
        "[]",
        '{"alive": true}',
        '{"config": "not-a-dict"}',
        '{"config": {"plan_dir": "/x"}}',
    ],
)
def test_a_malformed_fingerprint_warns_but_never_raises(tmp_path, raw_text):
    _write_raw_health(tmp_path, raw_text)

    result = _revision_result(tmp_path)

    assert result["status"] == "warn"
    assert result["message"]
    assert raw_text not in result["message"]


# --------------------------------------------------------------------------- #
# 8. A stale revision must never block startup.
# --------------------------------------------------------------------------- #
def test_the_check_never_reports_fail(tmp_path):
    _write_health(tmp_path, _config(tmp_path, checkout_behind_origin=3))
    assert _revision_result(tmp_path)["status"] in {"ok", "warn"}

    config = _config(tmp_path)
    config.pop("checkout_sha")
    config.pop("checkout_behind_origin")
    _write_health(tmp_path, config)
    assert _revision_result(tmp_path)["status"] in {"ok", "warn"}

    _write_raw_health(tmp_path, "{")
    assert _revision_result(tmp_path)["status"] in {"ok", "warn"}


# --------------------------------------------------------------------------- #
# 9. The recorded value is authoritative; git is never probed here.
# --------------------------------------------------------------------------- #
def test_it_reads_the_recorded_value_instead_of_probing_git(tmp_path):
    _write_health(tmp_path, _config(tmp_path, checkout_behind_origin=4))

    result = _revision_result(tmp_path, which=lambda _name: None)

    assert result["status"] == "warn"
    assert "4" in result["message"]


# --------------------------------------------------------------------------- #
# 10/11. Read-only: never modified, never created.
# --------------------------------------------------------------------------- #
def test_the_fingerprint_file_is_not_modified(tmp_path):
    path = _write_health(tmp_path, _config(tmp_path, checkout_behind_origin=2))
    before = path.read_bytes()

    _revision_result(tmp_path)

    assert path.read_bytes() == before


def test_a_missing_fingerprint_is_not_created(tmp_path):
    path = _health_path(tmp_path)
    assert not path.exists()

    _revision_result(tmp_path)

    assert not path.exists()


# --------------------------------------------------------------------------- #
# 12. The check is a module-level function in pipeline/preflight.py.
# --------------------------------------------------------------------------- #
def test_the_check_is_a_module_level_function_in_preflight():
    source = PREFLIGHT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    module_level = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_check_scheduler_revision"
    ]
    assert len(module_level) == 1, (
        "_check_scheduler_revision must be a module-level FunctionDef"
    )
    assert "SCHEDULER_REVISION" in source


# --------------------------------------------------------------------------- #
# 13. The new check is additive: the config check stays.
# --------------------------------------------------------------------------- #
def test_the_new_check_does_not_replace_the_config_check(tmp_path):
    _write_health(tmp_path, _config(tmp_path))

    names = [r.get("name") for r in _results(tmp_path)]

    assert "SCHEDULER_CONFIG" in names
    assert "SCHEDULER_REVISION" in names


# --------------------------------------------------------------------------- #
# 14. The briefed docstring updates (EDIT 1 and EDIT 4) are graded too.
# --------------------------------------------------------------------------- #
def test_the_docstrings_report_seven_checks():
    module_doc = ast.get_docstring(ast.parse(
        PREFLIGHT_PATH.read_text(encoding="utf-8")
    ))
    assert module_doc is not None
    assert "seven" in module_doc

    tree = ast.parse(PREFLIGHT_PATH.read_text(encoding="utf-8"))
    run_preflight = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_preflight"
    )
    assert "seven" in (ast.get_docstring(run_preflight) or "")
