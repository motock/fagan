"""Per-namespace worktree directories for a bound non-default namespace.

A process serving several namespaces must not let team A's story ``s1`` and
team B's story ``s1`` share a worktree directory. A bound non-default
namespace therefore gets its own ``_ns/<org>@<team>`` subtree; the default
namespace and an unconfigured deployment keep the historical layout.
"""

import contextlib
import json
from pathlib import Path

import pytest

from pipeline import actor
from pipeline import authorization as pauth
from pipeline import branch_names as bn
from pipeline import dispatch as pdisp

ENV = "PIPELINE_WORKTREE_NAMESPACE"


@pytest.fixture(autouse=True)
def _clear_namespace(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


# ---------- worktree_dir_name ----------


@pytest.mark.parametrize("namespace", [None, "default"])
def test_should_return_bare_key_for_default_namespace(namespace):
    assert bn.worktree_dir_name("ABC-1", namespace) == "ABC-1"


def test_should_preserve_key_case_in_default_namespace():
    assert bn.worktree_dir_name("Mixed-Case-Key", "default") == "Mixed-Case-Key"


def test_should_place_two_segment_namespace_under_reserved_dir():
    assert bn.worktree_dir_name("S1", "acme/web") == "_ns/acme@web/S1"


def test_should_place_single_segment_namespace_under_reserved_dir():
    assert bn.worktree_dir_name("S1", "acme") == "_ns/acme/S1"


def test_should_preserve_key_case_in_bound_namespace():
    assert bn.worktree_dir_name("Mixed-Case-Key", "acme/web") == (
        "_ns/acme@web/Mixed-Case-Key"
    )


def test_should_keep_key_named_like_reserved_dir_inside_bound_namespace():
    assert bn.worktree_dir_name("_ns", "acme/web") == "_ns/acme@web/_ns"


@pytest.mark.parametrize("key", ["_ns", "_NS", "_Ns"])
@pytest.mark.parametrize("namespace", [None, "default"])
def test_should_refuse_reserved_key_in_default_namespace(key, namespace):
    with pytest.raises(ValueError, match="story key is reserved"):
        bn.worktree_dir_name(key, namespace)


@pytest.mark.parametrize("bad", ["../x", "a/b/c", "", "A B"])
def test_should_refuse_invalid_namespace_without_echoing_it(bad):
    with pytest.raises(ValueError) as info:
        bn.worktree_dir_name("S1", bad)
    assert str(info.value) == "invalid namespace"


def test_should_keep_distinct_namespaces_with_same_key_apart():
    root = Path("/worktrees")
    first = root / bn.worktree_dir_name("x", "a")
    second = root / bn.worktree_dir_name("y", "a/x")
    assert first != second
    assert first not in second.parents
    assert second not in first.parents


def test_should_give_sibling_namespaces_distinct_directories():
    assert bn.worktree_dir_name("S1", "acme") != bn.worktree_dir_name("S1", "acme/web")


def test_should_return_today_result_for_any_namespace_when_env_set(monkeypatch):
    monkeypatch.setenv(ENV, "alice")
    expected = "alice/S1"
    assert bn.worktree_dir_name("S1") == expected
    assert bn.worktree_dir_name("S1", "default") == expected
    assert bn.worktree_dir_name("S1", "acme/web") == expected
    assert bn.worktree_dir_name("S1", "acme") == expected


def test_should_ignore_reserved_key_check_when_env_set(monkeypatch):
    monkeypatch.setenv(ENV, "alice")
    assert bn.worktree_dir_name("_ns", "acme/web") == "alice/_ns"


# ---------- story_worktree_path ----------


def test_should_return_derived_path_for_new_story(tmp_path):
    story = {"status": "todo"}
    assert bn.story_worktree_path(tmp_path, story, "S1", "acme/web") == (
        tmp_path / "_ns" / "acme@web" / "S1"
    )


def test_should_return_recorded_directory_inside_root(tmp_path):
    recorded = tmp_path / "_ns" / "acme@web" / "S1"
    recorded.mkdir(parents=True)
    story = {"worktree": str(recorded)}
    assert bn.story_worktree_path(tmp_path, story, "S1", "acme/web") == recorded


def test_should_return_legacy_recorded_directory_inside_root(tmp_path):
    recorded = tmp_path / "S1"
    recorded.mkdir()
    story = {"worktree": str(recorded)}
    assert bn.story_worktree_path(tmp_path, story, "S1", None) == recorded


def test_should_fall_back_when_recorded_directory_is_outside_root(tmp_path):
    root = tmp_path / "worktrees"
    outside = tmp_path / "elsewhere"
    outside.mkdir(parents=True)
    story = {"worktree": str(outside)}
    assert bn.story_worktree_path(root, story, "S1", "acme/web") == (
        root / "_ns" / "acme@web" / "S1"
    )


def test_should_fall_back_when_recorded_directory_does_not_exist(tmp_path):
    story = {"worktree": str(tmp_path / "_ns" / "acme@web" / "gone")}
    assert bn.story_worktree_path(tmp_path, story, "S1", "acme/web") == (
        tmp_path / "_ns" / "acme@web" / "S1"
    )


@pytest.mark.parametrize("value", [42, ["S1"], Path("/x")])
def test_should_fall_back_when_recorded_value_is_not_a_string(tmp_path, value):
    story = {"worktree": value}
    assert bn.story_worktree_path(tmp_path, story, "S1", None) == tmp_path / "S1"


# ---------- worktree_parent ----------


@pytest.mark.parametrize("namespace", [None, "default"])
def test_should_place_default_namespace_parent_at_root(tmp_path, namespace):
    assert bn.worktree_parent(tmp_path, namespace) == tmp_path


def test_should_place_parent_of_two_segment_namespace_under_reserved_dir(tmp_path):
    assert bn.worktree_parent(tmp_path, "acme/web") == tmp_path / "_ns" / "acme@web"


def test_should_place_parent_of_single_segment_namespace_under_reserved_dir(tmp_path):
    assert bn.worktree_parent(tmp_path, "acme") == tmp_path / "_ns" / "acme"


def test_should_keep_root_as_parent_when_env_set(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "alice")
    assert bn.worktree_parent(tmp_path, "acme/web") == tmp_path


def test_should_hold_the_derived_worktree_under_its_parent(tmp_path):
    parent = bn.worktree_parent(tmp_path, "acme/web")
    assert parent / "S1" == tmp_path / bn.worktree_dir_name("S1", "acme/web")


# ---------- dispatch wiring ----------


class _FakeStore:
    """Minimal stand-in for ``pipeline.server._store``.

    Dispatch reads the manifest and asks ``cross_plan`` about collisions; the
    collision check is forced to report one so dispatch returns before any git
    or worktree work.
    """

    def __init__(self, manifest_path, manifest):
        self._manifest_path = manifest_path
        self._manifest = manifest

    def transaction(self, plan_name):
        return contextlib.nullcontext(True)

    def manifest_path(self, plan_name):
        return self._manifest_path

    def get_manifest(self, plan_name):
        return self._manifest

    def records(self):
        return self

    def put_manifest(self, namespace, plan_name, manifest):
        self._manifest_path.write_text(json.dumps(manifest))
        self._manifest = manifest

    def get_manifests(self, namespace):
        return {}


@pytest.fixture
def dispatch_probe(tmp_path, monkeypatch):
    """Drive the real ``_dispatch_story_impl`` up to the collision check.

    Returns ``(worktree_root, captured)``; ``captured`` collects the
    ``worktrees`` argument handed to ``cross_plan.key_conflicts``. The audit
    sink is stubbed: the file audit log serves only the default namespace, and
    it is the storage boundary here, not the code under test.
    """
    monkeypatch.setattr(pauth, "record_audit", lambda *a, **k: None)
    worktree_root = tmp_path / "worktrees"
    manifest_path = tmp_path / "plans" / "plan-a" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest = {"stories": {"ABC-1": {"status": "todo"}}}
    monkeypatch.setattr(pdisp, "_store", _FakeStore(manifest_path, manifest))
    monkeypatch.setattr(pdisp, "WORKTREE_ROOT", worktree_root)
    monkeypatch.setattr(pdisp, "_notify_user", lambda *a, **k: None)

    captured = []

    def _fake_key_conflicts(keys, manifests, worktrees):
        captured.append(worktrees)
        return [{"plan": "other-plan", "other_key": "ABC-1"}]

    monkeypatch.setattr(pdisp.cross_plan, "key_conflicts", _fake_key_conflicts)
    return worktree_root, captured


def test_dispatch_should_hand_namespaced_parent_to_collision_check(dispatch_probe):
    worktree_root, captured = dispatch_probe
    with actor.bind_namespace("acme/web"):
        result = pdisp._dispatch_story_impl("plan-a", "ABC-1")
    assert result["ok"] is False  # the forced collision short-circuits
    assert captured == [worktree_root / "_ns" / "acme@web"]


def test_dispatch_should_keep_root_collision_check_for_default_namespace(dispatch_probe):
    worktree_root, captured = dispatch_probe
    result = pdisp._dispatch_story_impl("plan-a", "ABC-1")
    assert result["ok"] is False
    assert captured == [worktree_root]


def test_dispatch_should_refuse_invalid_bound_namespace_before_collision_check(
    dispatch_probe,
):
    _, captured = dispatch_probe
    with actor.bind_namespace("../x"):
        result = pdisp._dispatch_story_impl("plan-a", "ABC-1")
    assert result == {
        "ok": False,
        "error": "invalid namespace",
        "story_key": "ABC-1",
    }
    assert "../x" not in result["error"]
    assert captured == []


def test_dispatch_should_refuse_reserved_key_in_default_namespace(dispatch_probe):
    _, captured = dispatch_probe
    pdisp._store._manifest["stories"]["_ns"] = {"status": "todo"}
    result = pdisp._dispatch_story_impl("plan-a", "_ns")
    assert result == {
        "ok": False,
        "error": "story key is reserved",
        "story_key": "_ns",
    }
    assert captured == []


# ---------- worktree_or_refusal ----------


def test_should_return_refusal_for_reserved_key_in_default_namespace(tmp_path):
    assert bn.worktree_or_refusal(tmp_path, {}, "_ns") == (None, "story key is reserved")


def test_should_return_refusal_for_invalid_bound_namespace(tmp_path, monkeypatch):
    monkeypatch.setattr(actor, "current_namespace", lambda: "../x")
    assert bn.worktree_or_refusal(tmp_path, {}, "S1") == (None, "invalid namespace")


def test_should_return_path_for_valid_bound_namespace(tmp_path, monkeypatch):
    monkeypatch.setattr(actor, "current_namespace", lambda: "acme/web")
    assert bn.worktree_or_refusal(tmp_path, {}, "S1") == (
        tmp_path / "_ns" / "acme@web" / "S1",
        None,
    )


def test_should_unwrap_server_reference_root(tmp_path):
    class _Ref:
        def _value(self):
            return tmp_path

    assert bn.worktree_or_refusal(_Ref(), {}, "S1") == (tmp_path / "S1", None)


def test_should_let_misconfigured_env_namespace_propagate(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "Bad Name")
    with pytest.raises(ValueError) as info:
        bn.worktree_or_refusal(tmp_path, {}, "S1")
    assert not isinstance(info.value, bn.WorktreeRefused)
