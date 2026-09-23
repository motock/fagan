"""RPT-2: ``pipeline.advance._ServerRef`` must render its value, not a memory address.

``pipeline/advance.py`` defines its own private ``_ServerRef`` (the sibling copy in
``pipeline/service.py`` has ``__str__``/``__repr__``), so every operator-facing
message that interpolates one rendered ``<pipeline.advance._ServerRef object at
0x...>``. Four sites interpolate these refs: ``pipeline/advance.py`` (the
DISPATCH_MAX_ATTEMPTS retry notice) and ``pipeline/advance_merge.py`` x3 (the
MERGE_MAX_ATTEMPTS notices, reached through the ``_ModuleRef`` chain).

The import order below is load-bearing: ``pipeline.advance`` -> ``pipeline.dispatch``
-> ``pipeline.build_detect`` -> ``pipeline.server`` -> ``pipeline.advance`` is a
circular import, so ``pipeline.server`` must be primed first.
"""

import json  # noqa: I001  (the priming import below must stay first)

import pipeline.server  # primes the package before pipeline.advance

import pipeline.advance
import pipeline.advance_merge
import pipeline.concurrency as pcon
import pipeline.persistence as ppers
import pipeline.usage as pusage
from pipeline import server as p

# Every method the class had before this change; the two new renderers are added
# after the last of them (__getitem__) and none of these may be removed.
_SURVIVOR_METHODS = (
    "__init__",
    "_value",
    "__getattr__",
    "__call__",
    "__truediv__",
    "__contains__",
    "__iter__",
    "__sub__",
    "__eq__",
    "__ne__",
    "__lt__",
    "__le__",
    "__gt__",
    "__ge__",
    "__bool__",
    "__len__",
    "__getitem__",
)


def test_str_and_repr_match_the_server_copy():
    ref = pipeline.advance.DISPATCH_MAX_ATTEMPTS
    assert str(ref) == str(p.DISPATCH_MAX_ATTEMPTS)
    assert repr(ref) == repr(p.DISPATCH_MAX_ATTEMPTS)


def test_fstring_renders_the_value_not_an_address():
    ref = pipeline.advance.DISPATCH_MAX_ATTEMPTS
    rendered = f"{ref}"
    assert "0x" not in rendered
    assert rendered == str(ref._value())
    assert rendered == str(p.DISPATCH_MAX_ATTEMPTS)


def test_repr_renders_the_value_not_an_address():
    ref = pipeline.advance.DISPATCH_MAX_ATTEMPTS
    rendered = repr(ref)
    assert "0x" not in rendered
    assert rendered == repr(ref._value())


def test_advance_merge_module_ref_chain_renders_the_value():
    # The live bug was seen at this site: advance_merge reaches the same class
    # through _ModuleRef("pipeline.advance", "MERGE_MAX_ATTEMPTS").
    rendered = f"{pipeline.advance_merge.MERGE_MAX_ATTEMPTS}"
    assert "0x" not in rendered
    assert rendered == str(p.MERGE_MAX_ATTEMPTS)


def test_ref_resolves_at_call_time_not_import_time(monkeypatch):
    monkeypatch.setattr(p, "DISPATCH_MAX_ATTEMPTS", 7)
    assert str(pipeline.advance.DISPATCH_MAX_ATTEMPTS) == "7"
    assert repr(pipeline.advance.DISPATCH_MAX_ATTEMPTS) == "7"


def test_new_renderers_are_added_after_getitem_and_survivors_kept():
    cls = pipeline.advance._ServerRef
    members = list(cls.__dict__)
    for name in _SURVIVOR_METHODS:
        assert name in members, f"{name} was removed from _ServerRef"
    assert "__str__" in members
    assert "__repr__" in members
    # Placed immediately after the class's last pre-existing method.
    assert members.index("__getitem__") < members.index("__str__")
    assert members.index("__getitem__") < members.index("__repr__")


def test_module_level_ref_bindings_are_untouched():
    for name in ("DISPATCH_MAX_ATTEMPTS", "MERGE_MAX_ATTEMPTS"):
        ref = getattr(pipeline.advance, name)
        assert isinstance(ref, pipeline.advance._ServerRef)
        assert ref._name == name


def test_merge_gate_ci_fail_notice_renders_the_retry_cap(tmp_path, monkeypatch):
    # Behavioural grading at the real call site: drive the merge-gate CI-fail
    # path the way tests/unit/test_pipeline_mcp_server_usage_and_tools.py does
    # and assert the operator notice shows the cap instead of a memory address.
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(pusage, "USAGE_STATE_PATH", tmp_path / "usage_state.json")
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(ppers, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(pcon, "PLAN_DIR", plan_dir)
    (plan_dir / "cifail.manifest.json").write_text(
        json.dumps(
            {
                "epics": {},
                "stories": {
                    "P1": {
                        "summary": "approved",
                        "status": "pr_open",
                        "review_verdict": "APPROVE",
                        "risk": "low",
                        "worktree": "/x",
                    }
                },
            }
        )
    )
    monkeypatch.setattr(
        p,
        "_rebase_onto_master",
        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""},
    )
    monkeypatch.setattr(
        p, "_ci_status_once", lambda br, **_: {"state": "fail", "error": "boom"}
    )
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: None)
    notices = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg, **k: notices.append(msg))

    p.advance_pipeline("cifail")

    rework = [m for m in notices if "routed to rework" in m]
    assert rework, notices
    assert "(1/3)" in rework[0]
    assert "0x" not in rework[0]
