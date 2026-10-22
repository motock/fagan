"""AUTO-MERGE-2: ``PIPELINE_AUTO_MERGE=0`` parks every UNATTENDED merge.

``pipeline/merge.py::_merge_decision`` is the gate the scheduler's unattended
merge path (``advance._adjudicate_merges``) consults. ``PIPELINE_AUTO_MERGE``
is the operator's master switch for automatic merging; when it is off, no
unattended path may merge a story, whatever the autonomy mode, risk level or
review verdict.

The contract these tests pin down:

* With the flag off, an approved low-risk story in gated autonomy parks with
  the exact reason ``auto-merge disabled (PIPELINE_AUTO_MERGE=0)``.
* With the flag off, an approved high-risk story in full autonomy parks with
  that same reason BEFORE the high-risk adjudication runs: the adjudicator is
  never called and no adjudication record is appended. The store is left
  untouched, so re-enabling the flag on the same store adjudicates exactly
  once and records exactly once (no corruption, no double-append).
* The pre-existing "not approved" park still wins over the new guard: an
  unapproved story parks with reason ``not approved`` even when the flag is
  off. The guard must not mask that reason.
* The flag defaults on: unset (the parsed default) and the explicit ``"1"``
  both still merge an approved low-risk story in gated autonomy.
* The HUMAN approve path (``_approve_merge_impl``) never consults
  ``_merge_decision``, so the flag off does not block an explicit human merge.

``PIPELINE_AUTO_MERGE`` is imported function-locally from ``pipeline.config``
inside ``_merge_decision`` (deliberately NOT re-exported from
``pipeline.server``, which is at its 1,000-line cap), so the monkeypatch lands
on ``pipeline.config`` and is observed at call time.
"""

import contextlib

import pytest

from pipeline import config as config_mod
from pipeline import merge as merge_mod
from pipeline import plan_completion as plan_completion_mod
from pipeline import pr as pr_mod
from pipeline import server as server_mod

# pipeline/config.py: PIPELINE_AUTO_MERGE = os.environ.get("PIPELINE_AUTO_MERGE", "1") != "0"
# i.e. the flag is a parsed bool that defaults to True when unset.
DEFAULT_ON = True
OFF = False
DISABLED_REASON = "auto-merge disabled (PIPELINE_AUTO_MERGE=0)"
DISABLED_PARK = {"action": "park", "reason": DISABLED_REASON}


@pytest.fixture(autouse=True)
def _gated_autonomy(monkeypatch):
    """Baseline: gated autonomy at the ``low`` threshold, like production."""
    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(server_mod, "PIPELINE_RISK_THRESHOLD", "low")


def _story(**overrides):
    story = {
        "key": "AM2-1",
        "status": "pr_open",
        "review_verdict": "APPROVE",
        "risk": "low",
        "summary": "a story",
        "worktree": "",
    }
    story.update(overrides)
    return story


def _set_flag(monkeypatch, value):
    monkeypatch.setattr(config_mod, "PIPELINE_AUTO_MERGE", value)


# --------------------------------------------------------------------------
# 1. Flag off + APPROVE + low risk + gated autonomy -> park.
# --------------------------------------------------------------------------
def test_flag_off_parks_approved_low_risk_gated_story(monkeypatch):
    _set_flag(monkeypatch, OFF)

    result = merge_mod._merge_decision(_story(), "plan-a")

    assert result == DISABLED_PARK


# --------------------------------------------------------------------------
# 2. Flag off + APPROVE + high risk + full autonomy -> park BEFORE the
#    adjudicator, leaving the adjudication store untouched.
# --------------------------------------------------------------------------
def test_flag_off_parks_high_risk_full_autonomy_without_adjudicating(monkeypatch):
    _set_flag(monkeypatch, OFF)
    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "full")

    calls = []
    records = []

    def fake_adjudicate(story, plan_name=None):
        calls.append((story.get("key"), plan_name))
        records.append({"story_key": story.get("key"), "ruling": "proceed"})
        return {"action": "merge", "reason": "overlord ruled proceed"}

    monkeypatch.setattr(merge_mod, "_adjudicate_high_risk_merge", fake_adjudicate)

    high_risk = _story(risk="high")

    # Flag off: the guard returns before any adjudication.
    assert merge_mod._merge_decision(high_risk, "plan-a") == DISABLED_PARK
    assert len(calls) == 0
    assert len(records) == 0

    # State safety: re-enabling the flag on the SAME store adjudicates exactly
    # once and appends exactly one record - the off call left nothing behind.
    _set_flag(monkeypatch, DEFAULT_ON)
    assert merge_mod._merge_decision(high_risk, "plan-a") == {
        "action": "merge",
        "reason": "overlord ruled proceed",
    }
    assert len(calls) == 1
    assert len(records) == 1


# --------------------------------------------------------------------------
# 3. Flag off + not APPROVE -> "not approved" still wins.
# --------------------------------------------------------------------------
def test_flag_off_keeps_not_approved_reason(monkeypatch):
    _set_flag(monkeypatch, OFF)

    result = merge_mod._merge_decision(_story(review_verdict="REJECT"), "plan-a")

    assert result["action"] == "park"
    assert result["reason"] == "not approved"


# --------------------------------------------------------------------------
# 4. Flag unset (parsed default on) + APPROVE + low risk + gated -> merge.
# --------------------------------------------------------------------------
def test_flag_unset_defaults_on_and_merges(monkeypatch):
    # ``DEFAULT_ON`` mirrors what pipeline/config.py parses when the env var is
    # unset (``os.environ.get("PIPELINE_AUTO_MERGE", "1") != "0"`` -> True).
    # The function-local import reads the module attribute, so patching the
    # attribute is what the gate observes; setting the env var here would have
    # no effect on the outcome.
    _set_flag(monkeypatch, DEFAULT_ON)

    result = merge_mod._merge_decision(_story(), "plan-a")

    assert result["action"] == "merge"


# --------------------------------------------------------------------------
# 5. Explicit on + APPROVE + low risk + gated -> merge. ``True`` is what
#    pipeline/config.py actually produces; the literal "1" is the raw env
#    spelling and must also stay truthy.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("flag_value", [True, "1"])
def test_flag_explicit_one_merges(monkeypatch, flag_value):
    _set_flag(monkeypatch, flag_value)

    result = merge_mod._merge_decision(_story(), "plan-a")

    assert result["action"] == "merge"


# --------------------------------------------------------------------------
# 6. Flag off does not affect the human approve path.
# --------------------------------------------------------------------------
class _FakeStore:
    """Minimal ``pipeline.server._store`` stand-in for the approve path."""

    def __init__(self, manifest):
        self._manifest = manifest

    def manifest_path(self, plan_name):
        return f"/nonexistent/{plan_name}/manifest.json"

    @contextlib.contextmanager
    def transaction(self, plan_name):
        yield True

    def get_manifest(self, plan_name):
        return self._manifest


class _FakeSubprocess:
    """Stands in for the ``subprocess`` module inside ``pipeline.merge``.

    ``_approve_merge_impl`` shells out to ``git push`` / ``git rev-parse`` when
    the story has a real worktree directory; these tests must not touch git.
    """

    def __init__(self):
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs.get("cwd")))

        class _Done:
            returncode = 0
            stdout = "deadbeef\n"
            stderr = ""

        return _Done()


def test_flag_off_does_not_block_human_approve_merge(monkeypatch, tmp_path):
    _set_flag(monkeypatch, OFF)

    # The human path must never consult the unattended gate.
    def explode(*args, **kwargs):
        raise AssertionError("_approve_merge_impl must not call _merge_decision")

    monkeypatch.setattr(merge_mod, "_merge_decision", explode)

    worktree = tmp_path / "wt"
    worktree.mkdir()
    manifest = {
        "stories": {
            "AM2-1": {
                "key": "AM2-1",
                "status": "pr_open",
                "review_verdict": "APPROVE",
                "risk": "high",
                "worktree": str(worktree),
            }
        }
    }
    merged = []
    fake_subprocess = _FakeSubprocess()

    monkeypatch.setattr(merge_mod, "subprocess", fake_subprocess)
    monkeypatch.setattr(
        pr_mod, "_resolve_story_branch", lambda wt, key: f"agent/{key.lower()}"
    )
    monkeypatch.setattr(server_mod, "_store", _FakeStore(manifest))
    monkeypatch.setattr(server_mod, "_validate_key", lambda key: None)
    monkeypatch.setattr(
        server_mod, "_scoped_repo_root", lambda plan_name: contextlib.nullcontext()
    )
    monkeypatch.setattr(server_mod, "_default_branch", lambda: "master")
    monkeypatch.setattr(server_mod, "_rebase_onto_master", lambda wt, br: {"ok": True})
    monkeypatch.setattr(
        server_mod, "_ci_status", lambda branch, sha="": {"state": "pass", "error": ""}
    )
    monkeypatch.setattr(server_mod, "_ci_rerun", lambda sha: None)
    monkeypatch.setattr(
        server_mod, "_reverify_acceptance", lambda story, wt, key: {"state": "pass"}
    )
    monkeypatch.setattr(server_mod, "_reverify_build", lambda wt: {"state": "pass"})
    monkeypatch.setattr(
        server_mod, "_mcp_self_source_touched", lambda wt, base: None
    )
    monkeypatch.setattr(
        server_mod, "_merge_pr", lambda wt, key: merged.append((wt, key))
    )
    monkeypatch.setattr(server_mod, "_atomic_write_json", lambda path, data: None)
    monkeypatch.setattr(server_mod, "_mark_plane_done", lambda key, plan: None)
    monkeypatch.setattr(server_mod, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(server_mod, "_mcp_restart_notice", lambda touched: "")
    monkeypatch.setattr(
        plan_completion_mod, "notify_if_plan_completed", lambda plan, man: None
    )
    monkeypatch.setattr(merge_mod, "_maybe_record_retro", lambda plan, man: None)

    result = merge_mod._approve_merge_impl("plan-a", "AM2-1")

    assert result == {"ok": True, "story_key": "AM2-1", "status": "done"}
    assert merged == [(str(worktree), "AM2-1")]
    assert manifest["stories"]["AM2-1"]["status"] == "done"
