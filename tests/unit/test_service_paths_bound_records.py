"""The service, approval, CI, concurrency, grading and access paths read the
bound namespace instead of naming the default partition.

The scheduler advances each namespace inside ``pipeline.actor.bind_namespace``,
so every manifest read or write below a tick has to select the namespace bound
for that tick. ``pipeline.bound_records`` owns that selection once; the modules
covered here used to name the default partition themselves.

Two grades live in this file. The structural one scans the source of the eight
modules for a record-store call that still names the default partition, and for
the ``bound_records`` import that replaces it. The behavioural one drives the
public seams of those modules with a namespace bound and shows the plan that
lives only in that namespace is found, and the default partition is left alone.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from pipeline import access_hooks, ci, concurrency, detached_grade, service_role_config
from pipeline import server as server_module
from pipeline import service as service_module
from pipeline.actor import bind_namespace
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE

REPO_ROOT = Path(__file__).resolve().parents[2]

NAMESPACE = "a/b"
OTHER_NAMESPACE = "c/d"
PLAN = "plan-x"
STORY = "story-1"

#: The eight modules this story moves onto ``pipeline.bound_records``.
MODULES = (
    "pipeline/server.py",
    "pipeline/service.py",
    "pipeline/service_role_config.py",
    "pipeline/plan_approval.py",
    "pipeline/ci.py",
    "pipeline/concurrency.py",
    "pipeline/detached_grade.py",
    "pipeline/access_hooks.py",
)

#: The record-store calls this story replaces. The name is part of the pattern
#: so a call another story owns (``get_manifests``) is not graded here.
REPLACED_CALLS = ("list_plans", "get_manifest", "put_manifest")

#: ``records().list_plans("default")`` and friends: a record-store call that
#: still names the default partition.
DEFAULT_PARTITION_CALL = re.compile(
    r"records\(\)\s*\.\s*(?:" + "|".join(REPLACED_CALLS) + r")\s*\(\s*[\"']default[\"']"
)


def _source(module: str) -> str:
    return (REPO_ROOT / module).read_text(encoding="utf-8")


# -- the structural grade ------------------------------------------------------


@pytest.mark.parametrize("module", MODULES)
def test_no_record_store_call_in_the_module_names_the_default_partition(
    module: str,
) -> None:
    """None of the eight modules names the default partition any more."""
    offenders = DEFAULT_PARTITION_CALL.findall(_source(module))

    assert offenders == [], (
        f"{module} still names the default partition at {offenders!r}; read "
        "and write the bound namespace through pipeline.bound_records"
    )


@pytest.mark.parametrize("module", MODULES)
def test_the_module_uses_the_bound_record_wrappers(module: str) -> None:
    """Each of the eight modules reaches the record store through the wrappers."""
    assert "bound_records" in _source(module), (
        f"{module} must select the bound namespace through pipeline.bound_records"
    )


def test_the_scan_catches_a_default_partition_call_it_was_written_for() -> None:
    """The scanner fails on the shape of call this story replaces."""
    offender = DEFAULT_PARTITION_CALL.search(
        'x = store.records().list_plans("default")'
    )

    assert offender is not None
    assert "list_plans" in offender.group(0)


def test_the_scan_ignores_a_call_another_story_owns() -> None:
    """``get_manifests`` is not one of the calls this story replaces."""
    assert (
        DEFAULT_PARTITION_CALL.search('x = store.records().get_manifests("default")')
        is None
    )


# -- the behavioural grade -----------------------------------------------------


class _Store:
    """A store facade whose record view is one in-memory record store.

    The legacy ``get_manifest`` / ``save_manifest`` surface is namespace blind
    on purpose: it serves whichever partition holds the plan, so a test sees
    the *partition selection* and nothing else.
    """

    def __init__(self) -> None:
        self.records_ = InMemoryRecordStore()

    def records(self) -> InMemoryRecordStore:
        return self.records_

    def _find(self, plan_name: str) -> dict | None:
        for namespace in (NAMESPACE, OTHER_NAMESPACE, DEFAULT_NAMESPACE):
            manifest = self.records_.get_manifest(namespace, plan_name)
            if manifest is not None:
                return manifest
        return None

    def get_manifest(self, plan_name: str) -> dict[str, Any]:
        manifest = self._find(plan_name)
        if manifest is None:
            raise FileNotFoundError(f"no manifest for plan {plan_name!r}")
        return manifest

    def save_manifest(self, plan_name: str, manifest: dict[str, Any]) -> None:
        self.records_.put_manifest(DEFAULT_NAMESPACE, plan_name, manifest)

    def transaction(self, plan_name: str, wait_seconds: float = 0.0):
        class _Lock:
            def __enter__(self) -> bool:
                return True

            def __exit__(self, *exc: object) -> bool:
                return False

        return _Lock()


@pytest.fixture
def store() -> _Store:
    return _Store()


def _manifest(status: str = "todo", **extra: Any) -> dict[str, Any]:
    story = {"status": status, "summary": "s", "dependencies": [], "risk": "low"}
    story.update(extra)
    return {"plan": PLAN, "stories": {STORY: story}}


def test_service_list_ready_stories_finds_a_plan_in_the_bound_namespace(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``list_ready_stories`` looks the plan up in the bound namespace."""
    monkeypatch.setattr(server_module, "_store", store)
    store.records_.put_manifest(NAMESPACE, PLAN, _manifest())

    with bind_namespace(NAMESPACE):
        ready = service_module.PipelineService().list_ready_stories(PLAN)

    assert [entry["key"] for entry in ready] == [STORY]


def test_service_list_ready_stories_ignores_a_plan_only_in_the_default_namespace(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan that lives only in the default partition is not a bound plan."""
    monkeypatch.setattr(server_module, "_store", store)
    store.records_.put_manifest(DEFAULT_NAMESPACE, PLAN, _manifest())

    with bind_namespace(NAMESPACE):
        ready = service_module.PipelineService().list_ready_stories(PLAN)

    assert ready == []


def test_service_list_ready_stories_returns_nothing_for_an_unknown_plan(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan in no partition at all yields no ready stories."""
    monkeypatch.setattr(server_module, "_store", store)

    with bind_namespace(NAMESPACE):
        ready = service_module.PipelineService().list_ready_stories(PLAN)

    assert ready == []


def test_service_role_config_patch_finds_a_plan_in_the_bound_namespace(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The role-config patch reads the plan from the bound namespace."""
    monkeypatch.setattr(server_module, "_store", store)
    store.records_.put_manifest(NAMESPACE, PLAN, _manifest())

    with bind_namespace(NAMESPACE):
        result = service_role_config._patch_plan_impl(
            None, PLAN, {"role_config": {"dispatch": {"provider": "claude"}}}
        )

    assert result["ok"] is True


def test_service_role_config_patch_reports_a_plan_missing_from_the_bound_namespace(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan that lives only in the default partition is not found."""
    monkeypatch.setattr(server_module, "_store", store)
    store.records_.put_manifest(DEFAULT_NAMESPACE, PLAN, _manifest())

    with bind_namespace(NAMESPACE):
        result = service_role_config._patch_plan_impl(
            None, PLAN, {"role_config": {"dispatch": {"provider": "claude"}}}
        )

    assert result["ok"] is False
    assert "No such plan" in result["error"]


def test_service_role_config_set_finds_a_plan_in_the_bound_namespace(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-plan role config reads the plan from the bound namespace."""
    monkeypatch.setattr(server_module, "_store", store)
    store.records_.put_manifest(NAMESPACE, PLAN, _manifest())

    with bind_namespace(NAMESPACE):
        result = service_role_config._set_plan_role_config_impl(
            None, PLAN, "dispatch", "claude", None
        )

    assert result["ok"] is True


def test_service_role_config_set_reports_a_plan_missing_from_the_bound_namespace(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan that lives only in the default partition is not found."""
    monkeypatch.setattr(server_module, "_store", store)
    store.records_.put_manifest(DEFAULT_NAMESPACE, PLAN, _manifest())

    with bind_namespace(NAMESPACE):
        result = service_role_config._set_plan_role_config_impl(
            None, PLAN, "dispatch", "claude", None
        )

    assert result["ok"] is False
    assert "No such plan" in result["error"]


def test_ci_settings_read_the_manifest_of_the_bound_namespace(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CI gate context is built from the bound namespace's manifest."""
    monkeypatch.setattr(server_module, "_store", store)
    store.records_.put_manifest(
        NAMESPACE, PLAN, {"namespace": NAMESPACE, "team": "core"}
    )

    with bind_namespace(NAMESPACE):
        settings = ci.settings_for_plan(PLAN)

    assert settings.namespace == NAMESPACE
    assert settings.team == "core"


def test_ci_settings_fall_back_to_the_default_context_for_an_unknown_plan(
    monkeypatch: pytest.MonkeyPatch, store: _Store
) -> None:
    """A plan in no partition yields the default context, not an error."""
    monkeypatch.setattr(server_module, "_store", store)

    with bind_namespace(NAMESPACE):
        settings = ci.settings_for_plan(PLAN)

    assert settings.namespace == DEFAULT_NAMESPACE


def test_concurrency_reaps_a_zombie_in_the_bound_namespace(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The zombie reaper reads and writes the bound namespace's manifest."""
    monkeypatch.setattr(server_module, "_store", store)
    store.records_.put_manifest(
        NAMESPACE, PLAN, _manifest(status="in_progress", pid=999_999)
    )

    with bind_namespace(NAMESPACE):
        reaped = concurrency._reap_zombie_in_progress_stories()

    assert reaped == 1
    stored = store.records_.get_manifest(NAMESPACE, PLAN)
    assert stored["stories"][STORY]["status"] == "todo"
    assert "pid" not in stored["stories"][STORY]


def test_concurrency_reaping_leaves_the_default_partition_untouched(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default partition's manifest is never rewritten by a bound reap."""
    monkeypatch.setattr(server_module, "_store", store)
    store.records_.put_manifest(
        DEFAULT_NAMESPACE, PLAN, _manifest(status="in_progress", pid=999_999)
    )

    with bind_namespace(NAMESPACE):
        reaped = concurrency._reap_zombie_in_progress_stories()

    assert reaped == 0
    stored = store.records_.get_manifest(DEFAULT_NAMESPACE, PLAN)
    assert stored["stories"][STORY]["status"] == "in_progress"


def test_detached_grade_writes_the_manifest_into_the_bound_namespace(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The detached grade persists its transition into the bound namespace."""
    monkeypatch.setattr(server_module, "_store", store)
    manifest = _manifest(status="tests_passed")

    with bind_namespace(NAMESPACE):
        detached_grade._put_manifest(PLAN, manifest)

    assert store.records_.get_manifest(NAMESPACE, PLAN) is not None
    assert store.records_.get_manifest(DEFAULT_NAMESPACE, PLAN) is None


class _RefusingHook:
    """An access hook that refuses every dispatch."""

    def check(
        self, namespace: str, repo: str, action: str, risk: str
    ) -> access_hooks.AccessDecision:
        return access_hooks.AccessDecision(allowed=False, reason="policy_block")


def test_access_hooks_park_the_story_in_the_bound_namespace(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused dispatch parks the story in the bound namespace."""
    monkeypatch.setattr(access_hooks, "_hook", lambda: _RefusingHook())
    monkeypatch.setattr(access_hooks, "_manifest_repo", lambda manifest: "host/o/r")
    manifest = _manifest(status="todo")
    store.records_.put_manifest(NAMESPACE, PLAN, manifest)

    with bind_namespace(NAMESPACE):
        refusal = access_hooks.refuse_dispatch(store, PLAN, STORY, manifest)

    assert refusal is not None
    assert refusal["ok"] is False
    stored = store.records_.get_manifest(NAMESPACE, PLAN)
    assert stored["stories"][STORY]["status"] == "parked"
    assert store.records_.get_manifest(DEFAULT_NAMESPACE, PLAN) is None


def test_access_hooks_leave_the_default_partition_untouched(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused dispatch under a bound namespace writes no default manifest."""
    monkeypatch.setattr(access_hooks, "_hook", lambda: _RefusingHook())
    monkeypatch.setattr(access_hooks, "_manifest_repo", lambda manifest: "host/o/r")
    manifest = _manifest(status="todo")

    with bind_namespace(NAMESPACE):
        access_hooks.refuse_dispatch(store, PLAN, STORY, manifest)

    assert store.records_.get_manifest(DEFAULT_NAMESPACE, PLAN) is None
    assert store.records_.get_manifest(NAMESPACE, PLAN) is not None


def test_server_repo_root_reads_the_manifest_of_the_bound_namespace(
    store: _Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The plan's repo root is read from the bound namespace's manifest."""
    monkeypatch.setattr(server_module, "_store", store)
    repo_root = tmp_path / "repo"
    store.records_.put_manifest(
        NAMESPACE, PLAN, {"repo_root": str(repo_root), "stories": {}}
    )

    with bind_namespace(NAMESPACE):
        resolved = server_module._repo_root_for(PLAN)

    assert resolved == repo_root
