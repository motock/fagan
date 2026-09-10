"""TDD tests for the SCHEDULER_CONFIG startup preflight check.

Story: surface a scheduler config mismatch at STARTUP (run_preflight), not
only on demand (/api/health). The new check reads
``<resolved plan dir>/.scheduler_health.json`` and compares the fingerprint's
``config.plan_dir`` / ``config.worktree_root`` against what THIS process
resolved:

    agreement             -> "ok"   (message names the matching plan dir)
    divergence            -> "warn" (NOT "fail": a divergence must never
                                     block startup; message names both
                                     values and which field differs)
    file absent           -> "ok"   (a standalone or not-yet-started
                                     scheduler is a normal state; message
                                     says no fingerprint was found)
    malformed / unreadable-> "warn" (never an exception)

"warn" is already part of this module's emitted vocabulary (PLAN_DIR
does-not-exist-yet, local-backend CLI missing, unknown backend), so the new
check reuses it rather than inventing a third status.

run_preflight()'s result list is CUMULATIVE -- other checks are appended by
other code and more may be added later -- so every test locates THIS story's
entry by name and NEVER asserts the list's length, full contents, or order.

Until the check exists, every test here fails with a "no SCHEDULER_CONFIG
check in run_preflight() results" assertion -- the expected RED state for
this story.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from pipeline import paths, preflight

FINGERPRINT_FILENAME = ".scheduler_health.json"

_VOCABULARY = {"ok", "warn", "fail"}


# --------------------------------------------------------------------------- #
# Injected stubs: test the resolution logic, never today's host or registry.
# --------------------------------------------------------------------------- #
def _ok_which(name: str):
    """shutil.which stand-in: every CLI the checks look for is installed."""
    return f"/usr/local/bin/{name}"


def _ok_registry() -> dict:
    """registry_loader stub returning a well-formed (dict) registry."""
    return {"models": [], "roles": {}}


def _patch_worktree_root(monkeypatch, wt_root: Path) -> None:
    """Pin WORKTREE_ROOT everywhere the implementation may plausibly read it.

    pipeline.paths computes WORKTREE_ROOT from the env at import time, so the
    module attribute is patched (the repo's established pattern, e.g.
    tests/unit/_always_on_planner_helpers.py); the env var is set too, in
    case the check resolves it at call time, and any preflight-module
    binding is patched as well (mirrors test_preflight._patch_plan_dir).
    """
    monkeypatch.setenv("WORKTREE_ROOT", str(wt_root))
    monkeypatch.setattr(paths, "WORKTREE_ROOT", wt_root)
    if hasattr(preflight, "WORKTREE_ROOT"):
        monkeypatch.setattr(preflight, "WORKTREE_ROOT", wt_root)


def _write_fingerprint(plan_dir: Path, config: dict, extra: dict | None = None) -> Path:
    """Write a scheduler fingerprint in the shape the daemon produces.

    The outer payload carries the daemon's health keys; the check under test
    must only look at the ``config`` object, so extra outer keys prove it
    does not depend on an exact outer shape.
    """
    payload = {"alive": True, "reconcile_count": 3}
    if extra:
        payload.update(extra)
    payload["config"] = config
    fingerprint_path = plan_dir / FINGERPRINT_FILENAME
    fingerprint_path.write_text(json.dumps(payload), encoding="utf-8")
    return fingerprint_path


def _scheduler_entry(results: list) -> dict:
    """Locate THIS story's entry by name (the results list is cumulative)."""
    try:
        return next(r for r in results if r["name"] == "SCHEDULER_CONFIG")
    except StopIteration:
        pytest.fail(
            "no 'SCHEDULER_CONFIG' check in run_preflight() results; names="
            f"{[r.get('name') for r in results]}"
        )


def _run_preflight(tmp_path, monkeypatch, wt_root: Path | None = None):
    """run_preflight() against tmp_path with every host dependency stubbed."""
    if wt_root is None:
        wt_root = tmp_path / "worktree"
    _patch_worktree_root(monkeypatch, wt_root)
    return preflight.run_preflight(
        plan_dir=tmp_path, which=_ok_which, registry_loader=_ok_registry
    )


# --------------------------------------------------------------------------- #
# Contract guards: name, shape, signature, vocabulary.
# --------------------------------------------------------------------------- #
def test_run_preflight_signature_is_unchanged():
    """The story must not change run_preflight's signature."""
    sig = inspect.signature(preflight.run_preflight)
    assert list(sig.parameters) == ["plan_dir", "which", "registry_loader"]
    assert sig.parameters["plan_dir"].default is None
    assert sig.parameters["registry_loader"].default is None


def test_scheduler_config_entry_is_present_and_well_formed(tmp_path, monkeypatch):
    """MEMBERSHIP: the check exists, with exactly the documented dict shape.

    Membership only -- never the list's length, full contents, or order.
    """
    results = _run_preflight(tmp_path, monkeypatch)
    assert isinstance(results, list)
    assert "SCHEDULER_CONFIG" in {r["name"] for r in results}
    entry = _scheduler_entry(results)
    assert set(entry) == {"name", "status", "message"}
    assert entry["name"] == "SCHEDULER_CONFIG"
    assert entry["status"] in _VOCABULARY
    assert isinstance(entry["message"], str) and entry["message"]


# --------------------------------------------------------------------------- #
# Agreement -> ok.
# --------------------------------------------------------------------------- #
def test_matching_fingerprint_is_ok_and_message_names_plan_dir(tmp_path, monkeypatch):
    """Fingerprint agrees on both fields -> ok, naming the matching plan dir."""
    wt_root = tmp_path / "worktree"
    wt_root.mkdir()
    resolved_plan = str(Path(tmp_path).expanduser())
    fingerprint_path = _write_fingerprint(
        tmp_path,
        {"plan_dir": resolved_plan, "worktree_root": str(wt_root)},
    )
    before = fingerprint_path.read_text(encoding="utf-8")

    results = _run_preflight(tmp_path, monkeypatch, wt_root=wt_root)
    entry = _scheduler_entry(results)

    assert entry["status"] == "ok"
    assert resolved_plan in entry["message"]
    # The checks are read-only: the fingerprint file is untouched.
    assert fingerprint_path.read_text(encoding="utf-8") == before


def test_fingerprint_is_read_from_resolved_plan_dir(tmp_path, monkeypatch):
    """The fingerprint is read from the RESOLVED plan dir (explicit arg wins),
    not from pipeline.paths.PLAN_DIR."""
    other_plan_dir = tmp_path / "elsewhere"
    other_plan_dir.mkdir()
    monkeypatch.setattr(paths, "PLAN_DIR", other_plan_dir)
    if hasattr(preflight, "PLAN_DIR"):
        monkeypatch.setattr(preflight, "PLAN_DIR", other_plan_dir)
    wt_root = tmp_path / "worktree"
    _write_fingerprint(
        tmp_path,
        {
            "plan_dir": str(Path(tmp_path).expanduser()),
            "worktree_root": str(wt_root),
        },
    )

    results = preflight.run_preflight(
        plan_dir=tmp_path, which=_ok_which, registry_loader=_ok_registry
    )
    entry = _scheduler_entry(results)

    assert entry["status"] == "ok"


# --------------------------------------------------------------------------- #
# Divergence -> warn (never fail), naming both values and the differing field.
# --------------------------------------------------------------------------- #
def test_diverging_plan_dir_warns_and_names_both_values_and_field(
    tmp_path, monkeypatch
):
    wt_root = tmp_path / "worktree"
    wt_root.mkdir()
    scheduler_plan_dir = tmp_path / "scheduler-plan-dir"
    scheduler_plan_dir.mkdir()
    resolved_plan = str(Path(tmp_path).expanduser())
    assert scheduler_plan_dir != tmp_path
    _write_fingerprint(
        tmp_path,
        {"plan_dir": str(scheduler_plan_dir), "worktree_root": str(wt_root)},
    )

    results = _run_preflight(tmp_path, monkeypatch, wt_root=wt_root)
    entry = _scheduler_entry(results)

    assert entry["status"] == "warn"
    assert entry["status"] != "fail"
    # Both values are named: what this process resolved AND what the
    # scheduler wrote.
    assert resolved_plan in entry["message"]
    assert str(scheduler_plan_dir) in entry["message"]
    # And which field differs.
    assert "plan_dir" in entry["message"]


def test_diverging_worktree_root_warns_and_names_both_values_and_field(
    tmp_path, monkeypatch
):
    wt_root = tmp_path / "worktree"
    scheduler_wt_root = tmp_path / "scheduler-worktree"
    resolved_plan = str(Path(tmp_path).expanduser())
    _write_fingerprint(
        tmp_path,
        {"plan_dir": resolved_plan, "worktree_root": str(scheduler_wt_root)},
    )

    results = _run_preflight(tmp_path, monkeypatch, wt_root=wt_root)
    entry = _scheduler_entry(results)

    assert entry["status"] == "warn"
    assert entry["status"] != "fail"
    assert str(wt_root) in entry["message"]
    assert str(scheduler_wt_root) in entry["message"]
    assert "worktree_root" in entry["message"]


def test_divergence_does_not_block_startup(tmp_path, monkeypatch):
    """A divergence is a warn: raise_on_failure() must NOT raise on it."""
    _write_fingerprint(
        tmp_path,
        {"plan_dir": str(tmp_path / "scheduler-plan-dir"), "worktree_root": "x"},
    )

    results = _run_preflight(tmp_path, monkeypatch)
    entry = _scheduler_entry(results)

    assert entry["status"] == "warn"
    assert preflight.raise_on_failure(results) is None


# --------------------------------------------------------------------------- #
# File absent -> ok (a standalone / not-yet-started scheduler is normal).
# --------------------------------------------------------------------------- #
def test_missing_fingerprint_file_is_ok_says_none_found_and_does_not_raise(
    tmp_path, monkeypatch
):
    """NEGATIVE: no fingerprint file -> ok, none-found message, no raise.

    run_preflight() returning normally (rather than raising) is itself the
    does-not-raise assertion; the isinstance check pins a list came back.
    """
    assert not (tmp_path / FINGERPRINT_FILENAME).exists()

    results = _run_preflight(tmp_path, monkeypatch)

    assert isinstance(results, list)
    entry = _scheduler_entry(results)
    assert entry["status"] == "ok"
    lowered = entry["message"].lower()
    assert "fingerprint" in lowered
    assert any(
        token in lowered for token in ("no ", "not found", "absent", "none")
    ), entry["message"]


# --------------------------------------------------------------------------- #
# Malformed / unreadable -> warn, never an exception.
# --------------------------------------------------------------------------- #
def test_malformed_json_warns_does_not_raise_and_does_not_leak_contents(
    tmp_path, monkeypatch
):
    """NEGATIVE: invalid JSON -> warn, no exception, contents never embedded."""
    fingerprint_path = tmp_path / FINGERPRINT_FILENAME
    fingerprint_path.write_bytes(
        b'{"config": {"plan_dir": "LEAKMARKER-7f3a',  # truncated, invalid
    )

    results = _run_preflight(tmp_path, monkeypatch)

    entry = _scheduler_entry(results)
    assert entry["status"] == "warn"
    assert entry["status"] != "fail"
    assert "LEAKMARKER-7f3a" not in entry["message"]


def test_unreadable_fingerprint_warns_and_does_not_raise(tmp_path, monkeypatch):
    """A directory at the fingerprint path is unreadable -> warn, no raise.

    (Portable stand-in for a chmod-0 file, which root could still read.)
    """
    (tmp_path / FINGERPRINT_FILENAME).mkdir()

    results = _run_preflight(tmp_path, monkeypatch)

    entry = _scheduler_entry(results)
    assert entry["status"] == "warn"
    assert entry["status"] != "fail"


@pytest.mark.parametrize(
    "payload",
    [
        {"alive": True},  # valid JSON, but no config object at all
        {"alive": True, "config": ["not", "a", "dict"]},
        {"alive": True, "config": {}},  # config present but no comparable fields
    ],
    ids=["no-config-key", "config-not-a-dict", "config-empty"],
)
def test_unusable_config_object_warns_and_does_not_raise(
    tmp_path, monkeypatch, payload
):
    """A file that parses but carries no usable config is a MALFORMED
    fingerprint, not a benign absence: the file exists (a scheduler wrote
    something), so only the absent-FILE case gets the ok/none-found path.
    """
    (tmp_path / FINGERPRINT_FILENAME).write_text(
        json.dumps(payload), encoding="utf-8"
    )

    results = _run_preflight(tmp_path, monkeypatch)

    entry = _scheduler_entry(results)
    assert entry["status"] == "warn"
    assert entry["status"] != "fail"