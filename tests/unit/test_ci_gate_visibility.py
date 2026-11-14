"""TDD tests: make a disabled merge-CI gate loudly visible (visibility only).

The story adds exactly two production behaviours, and these tests pin both
without touching the gate's default, its behaviour, or ``_ci_status``'s
return shape:

1. ``pipeline/preflight.py`` gains a new warn-level check named
   ``"merge CI gate"``, positioned AFTER the dispatch-backend check and
   BEFORE the model-registry check, reading ``PIPELINE_MERGE_CI_GATE`` at
   CALL time (like the dispatch-backend check): ``"0"`` (after
   ``strip()``) -> status ``"warn"`` with a message naming the env var;
   anything else -> status ``"ok"``. Never ``"fail"``: disabling the gate
   is a legitimate operator choice during a CI outage.
2. ``pipeline/ci.py`` logs a WARNING through a module logger at BOTH
   ``if not PIPELINE_MERGE_CI_GATE:`` early-return sites (``_ci_status``
   and ``_ci_status_once``) while returning the exact original dicts.

These tests are intentionally RED until that implementation exists. The
environment is stubbed exclusively with ``monkeypatch`` -- nothing here
asserts against the ambient environment (see
.claude/rules/testing-config-gates.md; this repo hit that bug live).

Note on shared artifacts: preflight's result list is extended by multiple
stories over time (checks a-e already), so these tests assert MEMBERSHIP
and ORDER relative to fixed anchors ("dispatch backend", "model
registry") and never the total number of checks.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

# pipeline.server is imported explicitly; import order is not required for
# cold-importability (every pipeline module imports cold, see
# tests/unit/test_hub_satellite_cold_imports.py).
import pipeline.server  # noqa: F401
from pipeline import ci, preflight

GATE_VAR = "PIPELINE_MERGE_CI_GATE"
CHECK_NAME = "merge CI gate"
DISABLED_RETURN = {"state": "pass", "error": "CI gate disabled"}
BRANCH = "feature/ci-gate-visibility"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _ok_which(tool: str) -> str:
    """Every tool the preflight checks look for is 'installed'."""
    return f"/usr/bin/{tool}"


def _ok_registry() -> dict:
    """A well-formed (JSON-object-shaped) model registry payload."""
    return {"roles": {"dispatch": {"provider": "claude"}}}


@pytest.fixture(autouse=True)
def _isolated_gate_env(monkeypatch, seed_merge_gate):
    """Scrub every env var the touched code paths consult, and isolate the Store.

    The gate is a tenant setting now: each test seeds exactly the record it
    needs via ``seed_merge_gate``. The ambient environment (which, inside a
    dispatched agent's bash tool, may carry PIPELINE_MERGE_CI_GATE=0) must
    never influence an assertion, and the Store must never touch a real plan
    directory.
    """
    for var in (GATE_VAR, "PIPELINE_BACKEND_DISPATCH", "PLAN_DIR", "WORKTREE_ROOT"):
        monkeypatch.delenv(var, raising=False)


def _run_preflight(tmp_path):
    """Same call shape the existing preflight tests use."""
    return preflight.run_preflight(
        plan_dir=tmp_path, which=_ok_which, registry_loader=_ok_registry
    )


def _find(results, name):
    matches = [entry for entry in results if entry.get("name") == name]
    assert matches, (
        f"no preflight check named {name!r}; result names: "
        f"{[entry.get('name') for entry in results]}"
    )
    return matches[0]


# --------------------------------------------------------------------------- #
# Change 1: the preflight check.
# --------------------------------------------------------------------------- #
def test_preflight_warns_when_merge_ci_gate_is_disabled(tmp_path, seed_merge_gate):
    seed_merge_gate(merge_ci_gate=False)
    entry = _find(_run_preflight(tmp_path), CHECK_NAME)
    assert entry["status"] == "warn"


def test_preflight_reports_ok_when_gate_is_enabled(tmp_path, seed_merge_gate):
    # Default path: no merge_ci_gate record is seeded, so the safe value stands.
    entry = _find(_run_preflight(tmp_path), CHECK_NAME)
    assert entry["status"] == "ok"


def test_preflight_reports_ok_when_gate_explicitly_enabled(tmp_path, seed_merge_gate):
    seed_merge_gate(merge_ci_gate=True)
    entry = _find(_run_preflight(tmp_path), CHECK_NAME)
    assert entry["status"] == "ok"


def test_disabled_gate_message_names_the_setting(tmp_path, seed_merge_gate):
    seed_merge_gate(merge_ci_gate=False)
    entry = _find(_run_preflight(tmp_path), CHECK_NAME)
    assert "merge_ci_gate" in entry["message"], (
        "the warn message must name the merge_ci_gate setting so the operator "
        "knows what to turn back on"
    )


def test_disabled_gate_is_warn_never_fail(tmp_path, seed_merge_gate):
    seed_merge_gate(merge_ci_gate=False)
    results = _run_preflight(tmp_path)
    entry = _find(results, CHECK_NAME)
    assert entry["status"] == "warn"
    assert entry["status"] != "fail"
    # A warn must never block work: raise_on_failure() raises only on
    # fail-status checks, so a disabled gate must not raise here.
    preflight.raise_on_failure(results)


def test_preflight_ignores_the_legacy_env_var_once_the_setting_is_in_the_store(
    tmp_path, monkeypatch, seed_merge_gate
):
    # The Store is the source of truth. A legacy env value (here padded, which
    # the old reader treated as a disable) must not flip the check on its own.
    monkeypatch.setenv(GATE_VAR, " 0 ")
    entry = _find(_run_preflight(tmp_path), CHECK_NAME)
    assert entry["status"] == "ok", (
        "PIPELINE_MERGE_CI_GATE must not control the gate any more; only the "
        "merge_ci_gate setting does"
    )




def test_disabled_gate_message_says_merges_can_land_on_red(tmp_path, seed_merge_gate):
    seed_merge_gate(merge_ci_gate=False)
    entry = _find(_run_preflight(tmp_path), CHECK_NAME)
    message = entry["message"]
    assert "red" in message.lower(), "message must say merges can land on red"
    assert re.search(r"\bnot\b", message, re.IGNORECASE), (
        "message must state plainly that the gate will NOT consult CI"
    )
    assert "ci" in message.lower()


def test_disabled_gate_message_mentions_temporary_workaround(
    tmp_path, seed_merge_gate
):
    seed_merge_gate(merge_ci_gate=False)
    entry = _find(_run_preflight(tmp_path), CHECK_NAME)
    message = entry["message"].lower()
    assert any(
        word in message for word in ("temporary", "outage", "workaround")
    ), "message must say this is usually a temporary workaround (e.g. a CI outage)"
    assert any(
        word in message for word in ("unset", "restore")
    ), "message must say how to restore the gate (unset the env var)"


def test_ok_message_says_gate_requires_green_ci(tmp_path, seed_merge_gate):
    entry = _find(_run_preflight(tmp_path), CHECK_NAME)
    message = entry["message"].lower()
    assert "green" in message, "ok message must confirm green CI is required"
    assert "ci" in message


def test_new_check_entry_matches_result_shape(tmp_path, seed_merge_gate):
    seed_merge_gate(merge_ci_gate=False)
    entry = _find(_run_preflight(tmp_path), CHECK_NAME)
    assert set(entry) == {"name", "status", "message"}
    assert entry["status"] in {"ok", "warn", "fail"}
    assert isinstance(entry["message"], str) and entry["message"]


def test_new_check_sits_between_dispatch_and_registry_checks(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(GATE_VAR, raising=False)
    names = [entry["name"] for entry in _run_preflight(tmp_path)]
    assert names.index("dispatch backend") < names.index(CHECK_NAME), (
        "the merge CI gate check must come after the dispatch-backend check"
    )
    assert names.index(CHECK_NAME) < names.index("model registry"), (
        "the merge CI gate check must come before the model-registry check"
    )


def test_new_check_follows_the_check_comment_style():
    source = (REPO_ROOT / "pipeline" / "preflight.py").read_text(encoding="utf-8")
    check_comments = re.findall(r"^\s*#\s*--\s*check\b.*$", source, re.MULTILINE)
    assert check_comments, "preflight.py lost its '-- check x: ... --' comments"
    assert any(
        re.search(r"merge|ci", comment, re.IGNORECASE)
        for comment in check_comments
    ), "no '-- check ...' comment covers the merge CI gate check"


# --------------------------------------------------------------------------- #
# Change 2: the log lines in pipeline/ci.py.
# --------------------------------------------------------------------------- #
def test_ci_module_has_a_module_logger():
    assert hasattr(ci, "logger"), (
        "pipeline/ci.py needs a module logger: logger = logging.getLogger(__name__)"
    )
    assert isinstance(ci.logger, logging.Logger)
    assert ci.logger.name == "pipeline.ci"
    source = (REPO_ROOT / "pipeline" / "ci.py").read_text(encoding="utf-8")
    assert "import logging" in source
    assert "getLogger(__name__)" in source


def test_ci_status_logs_a_warning_when_gate_disabled(monkeypatch, caplog, seed_merge_gate):
    seed_merge_gate(merge_ci_gate=False)
    with caplog.at_level(logging.WARNING, logger="pipeline.ci"):
        result = ci._ci_status(BRANCH, sha="", settings=ci.context_from_manifest({}))
    # The log line was ADDED; the contract is byte-for-byte unchanged.
    assert result == DISABLED_RETURN
    records = [
        record
        for record in caplog.records
        if record.name == "pipeline.ci" and record.levelno == logging.WARNING
    ]
    assert records, (
        "no WARNING logged through pipeline.ci's logger when the gate is "
        "disabled (_ci_status site)"
    )
    message = records[-1].getMessage().lower()
    assert "disabled" in message
    assert "merge" in message
    assert "without" in message, "message must say the merge proceeds without checking CI"


def test_ci_status_once_logs_a_warning_when_gate_disabled(monkeypatch, caplog, seed_merge_gate):
    seed_merge_gate(merge_ci_gate=False)
    with caplog.at_level(logging.WARNING, logger="pipeline.ci"):
        result = ci._ci_status_once(
            BRANCH, sha="", settings=ci.context_from_manifest({})
        )
    assert result == DISABLED_RETURN
    records = [
        record
        for record in caplog.records
        if record.name == "pipeline.ci" and record.levelno == logging.WARNING
    ]
    assert records, (
        "no WARNING logged through pipeline.ci's logger when the gate is "
        "disabled (_ci_status_once site)"
    )
    message = records[-1].getMessage().lower()
    assert "disabled" in message
    assert "without" in message


def test_gate_default_is_enabled_when_no_setting_is_stored(seed_merge_gate):
    # Nothing seeded: the Store's safe value (ON) stands for every context.
    assert ci.context_from_manifest({}).value("merge_ci_gate") is True
