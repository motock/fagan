"""Per-tick dispatch cap: PIPELINE_MAX_DISPATCH_PER_TICK.

Incident background (2026-09-11): a single tick for plan `chat-logs-usage`
dispatched three `:cloud` stories in a row, because cloud-backed dispatch
bypasses the MAX_CONCURRENT_AGENTS device-slot cap entirely. Each dispatch
pays a synchronous ``_run_test_author_phase`` + ``_run_planner`` call while
the tick holds the plan's ``_plan_lock``, so the lock was held for ~35
minutes and an external ``approve_merge`` was refused with
``plan busy (scheduler tick in progress)``.

These tests pin the additional, independent per-plan bound:
``PIPELINE_MAX_DISPATCH_PER_TICK`` (default 1; ``<= 0`` means no cap;
a malformed env value degrades to the default instead of raising). The
``free_device_slots`` / ``MAX_CONCURRENT_AGENTS`` math, the per-story
resource gate, the ``gated`` list and the cloud-slot exemption are out of
scope for this story; they are pinned wide open here only so they cannot
interfere with the cap assertions.

Per .claude/rules/testing-config-gates.md, every config value is stubbed per
test with a synthetic value -- never assert today's configured environment.
The single permitted live-value assertion is the hardcoded fallback: with
the env var absent, the default is 1.
"""

import importlib
import json

import pytest

import pipeline.advance as advance_module
import pipeline.config as config_module

ENV_VAR = "PIPELINE_MAX_DISPATCH_PER_TICK"
_PLAN = "cap-plan"


# ---------------------------------------------------------------------------
# Config-resolution helpers


def _reload_config_with(monkeypatch, env_value):
    """Reload pipeline.config with ENV_VAR set to env_value (None = unset)."""
    if env_value is None:
        monkeypatch.delenv(ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(ENV_VAR, env_value)
    return importlib.reload(config_module)


@pytest.fixture(autouse=True)
def _restore_config_module():
    """importlib.reload mutates the shared config module; put it back."""
    yield
    importlib.reload(config_module)


# ---------------------------------------------------------------------------
# Tick-level seams. Only config values (per testing-config-gates.md) and the
# process-heavy collaborators a dispatch pays for are stubbed; the real tick
# body and its real `for key in ready:` loop run. _advance_pipeline_locked_impl
# is the canonical tick seam (the triage-sweep tests monkeypatch it too);
# calling it directly skips the advisory triage/wedge sweeps, which shell out
# and are irrelevant to the cap.


class _DispatchRecorder:
    """Stands in for the dispatch_story server ref; records (plan, key)."""

    def __init__(self, fail_first=0):
        self.calls = []
        self.fail_first = fail_first

    def __call__(self, plan_name, key):
        self.calls.append((plan_name, key))
        if len(self.calls) <= self.fail_first:
            # The structured git-setup failure shape: ok False, no launch.
            return {"ok": False, "error": "git setup failed"}
        return {"ok": True}


class _FakeBackend:
    """Per-story resource gate always reports healthy."""

    def get_backend(self, *_args, **_kwargs):
        return self

    def resource_status(self, **_kwargs):
        return {"ok": True}


class _FakeAutonomy:
    """Stands in for the PIPELINE_AUTONOMY _ServerRef proxy.

    The tick calls ``PIPELINE_AUTONOMY._value()``; a host with
    PIPELINE_AUTONOMY=dry-run in its environment would otherwise return a
    dry_run dict and never dispatch, breaking every tick test for reasons
    unrelated to the cap.
    """

    def __init__(self, value="gated"):
        self._value_ = value

    def _value(self):
        return self._value_


class _FakeStore:
    """Backs the tick's _store seam with a real JSON file on disk.

    The tick body re-reads the manifest from disk between phases and writes
    it back through _atomic_write_json, so the seam must be a real file.
    """

    def __init__(self, path):
        self.path = path

    def manifest_path(self, plan_name):
        return self.path

    def get_manifest(self, plan_name):
        return json.loads(self.path.read_text())


def _seed_tick(monkeypatch, tmp_path, story_keys, statuses=None, fail_first=0):
    """Write a plan manifest whose stories are dispatch-eligible and wire
    every seam the tick body touches. Returns (recorder, manifest_path)."""
    statuses = statuses or {}
    stories = {}
    for key in story_keys:
        stories[key] = {
            "key": key,
            "title": f"story {key}",
            "status": statuses.get(key, "todo"),
            "risk": "low",
            "dispatch_attempts": 0,
        }
    manifest = {"name": _PLAN, "paused": False, "stories": stories}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))

    monkeypatch.setattr(advance_module, "_store", _FakeStore(path))
    monkeypatch.setattr(advance_module, "backend", _FakeBackend())
    # The blanket dispatch/review gate: a resource-poor host would otherwise
    # interrupt the fixtures' in-progress stories and gate every dispatch.
    monkeypatch.setattr(
        advance_module, "_role_resource_ok", lambda *a, **k: (True, "")
    )
    # Cross-plan on-device slot count reads the REAL PLAN_DIR via glob; pin it
    # to zero so the (out-of-scope) device-slot math can never bind here.
    monkeypatch.setattr(
        advance_module,
        "_count_on_device_in_progress_agents",
        lambda: 0,
        raising=True,
    )
    # Autonomy must not be dry-run on the host running the tests.
    monkeypatch.setattr(
        advance_module, "PIPELINE_AUTONOMY", _FakeAutonomy("gated"), raising=True
    )
    monkeypatch.setattr(advance_module, "_adjudicate_merges", lambda *a, **k: None)
    monkeypatch.setattr(advance_module, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(
        advance_module, "interrupt_story", lambda *a, **k: {"ok": True}
    )
    monkeypatch.setattr(
        advance_module, "check_story_status", lambda *a, **k: {"ok": True}
    )
    monkeypatch.setattr(advance_module, "review_story", lambda *a, **k: {"ok": True})

    rec = _DispatchRecorder(fail_first=fail_first)

    def _fake_dispatch(plan_name, key):
        outcome = rec(plan_name, key)
        if outcome.get("ok"):
            # Mimic the real launcher: mark the story in flight on disk. No
            # pid, so the tick's poll/interrupt loops skip it, and the next
            # tick's ready computation does not re-pick it.
            m = json.loads(path.read_text())
            m["stories"][key]["status"] = "in_progress"
            advance_module._atomic_write_json(path, m)
        return outcome

    monkeypatch.setattr(advance_module, "dispatch_story", _fake_dispatch)
    return rec, path


def _pin_tick_environment(monkeypatch):
    """Pin every out-of-scope gate wide open so only the cap can bind."""
    # Device-slot math must never be the binding constraint in these tests.
    monkeypatch.setattr(config_module, "MAX_CONCURRENT_AGENTS", 8, raising=False)
    if hasattr(advance_module, "MAX_CONCURRENT_AGENTS"):
        monkeypatch.setattr(advance_module, "MAX_CONCURRENT_AGENTS", 8, raising=False)
    # The dispatch error budget must not eat the fixtures' first attempts.
    monkeypatch.setattr(config_module, "DISPATCH_MAX_ATTEMPTS", 3, raising=False)
    if hasattr(advance_module, "DISPATCH_MAX_ATTEMPTS"):
        monkeypatch.setattr(advance_module, "DISPATCH_MAX_ATTEMPTS", 3, raising=False)
    # Usage gate: thresholds above 100% can never pause the tick.
    for name in (
        "SESSION_PAUSE_THRESHOLD",
        "SESSION_RESUME_THRESHOLD",
        "WEEK_PAUSE_THRESHOLD",
        "WEEK_RESUME_THRESHOLD",
    ):
        monkeypatch.setattr(config_module, name, 101, raising=False)
        if hasattr(advance_module, name):
            monkeypatch.setattr(advance_module, name, 101, raising=False)


def _set_cap(monkeypatch, value):
    """Force the cap through every resolution path the tick may read it from
    (env re-read, config attribute, from-import binding in advance)."""
    monkeypatch.setenv(ENV_VAR, str(value))
    monkeypatch.setattr(
        config_module, "PIPELINE_MAX_DISPATCH_PER_TICK", value, raising=False
    )
    if hasattr(advance_module, "PIPELINE_MAX_DISPATCH_PER_TICK"):
        monkeypatch.setattr(
            advance_module, "PIPELINE_MAX_DISPATCH_PER_TICK", value, raising=False
        )


def _run_tick(plan_name=_PLAN):
    return advance_module._advance_pipeline_locked_impl(plan_name)


def _read_stories(path):
    return json.loads(path.read_text())["stories"]


def _deferred_keys(path, dispatched):
    return [k for k in _read_stories(path) if k not in dispatched]


# ---------------------------------------------------------------------------
# Config resolution (pipeline/config.py)


class TestConfigResolution:
    def test_env_unset_falls_back_to_hardcoded_default_of_one(self, monkeypatch):
        # The one permitted live-value assertion: the hardcoded fallback.
        cfg = _reload_config_with(monkeypatch, None)
        assert cfg.PIPELINE_MAX_DISPATCH_PER_TICK == 1

    def test_env_override_is_honoured(self, monkeypatch):
        cfg = _reload_config_with(monkeypatch, "2")
        assert cfg.PIPELINE_MAX_DISPATCH_PER_TICK == 2

    def test_zero_parses_through_as_the_no_cap_value(self, monkeypatch):
        cfg = _reload_config_with(monkeypatch, "0")
        assert cfg.PIPELINE_MAX_DISPATCH_PER_TICK == 0

    def test_negative_value_parses_through(self, monkeypatch):
        cfg = _reload_config_with(monkeypatch, "-1")
        assert cfg.PIPELINE_MAX_DISPATCH_PER_TICK == -1

    def test_malformed_value_degrades_to_default_without_raising(self, monkeypatch):
        cfg = _reload_config_with(monkeypatch, "abc")
        assert cfg.PIPELINE_MAX_DISPATCH_PER_TICK == 1

    def test_empty_value_degrades_to_default_without_raising(self, monkeypatch):
        cfg = _reload_config_with(monkeypatch, "")
        assert cfg.PIPELINE_MAX_DISPATCH_PER_TICK == 1


# ---------------------------------------------------------------------------
# Tick behaviour (pipeline/advance.py dispatch loop)


class TestPerTickCap:
    def test_case1_default_dispatches_exactly_one_story_per_tick(
        self, monkeypatch, tmp_path
    ):
        # Hardcoded fallback invariant (the one permitted live-value
        # assertion): with the env var absent the default is 1.
        monkeypatch.delenv(ENV_VAR, raising=False)
        importlib.reload(config_module)
        assert config_module.PIPELINE_MAX_DISPATCH_PER_TICK == 1
        _pin_tick_environment(monkeypatch)
        _set_cap(monkeypatch, 1)
        rec, path = _seed_tick(monkeypatch, tmp_path, ["s1", "s2", "s3"])

        _run_tick()

        assert len(rec.calls) == 1
        deferred = _deferred_keys(path, {k for _, k in rec.calls})
        assert len(deferred) == 2
        for key in deferred:
            story = _read_stories(path)[key]
            assert story["status"] == "todo"
            assert story["dispatch_attempts"] == 0
            assert story["status"] not in ("gated", "parked", "failed")

    def test_case2_three_consecutive_ticks_dispatch_all_three_stories(
        self, monkeypatch, tmp_path
    ):
        _pin_tick_environment(monkeypatch)
        _set_cap(monkeypatch, 1)
        rec, path = _seed_tick(monkeypatch, tmp_path, ["s1", "s2", "s3"])

        _run_tick()
        assert len(rec.calls) == 1
        _run_tick()
        assert len(rec.calls) == 2
        _run_tick()
        assert len(rec.calls) == 3

        assert sorted(k for _, k in rec.calls) == ["s1", "s2", "s3"]
        assert path.exists()

    def test_case3_cap_of_two_dispatches_exactly_two_of_three(
        self, monkeypatch, tmp_path
    ):
        _pin_tick_environment(monkeypatch)
        _set_cap(monkeypatch, 2)
        rec, path = _seed_tick(monkeypatch, tmp_path, ["s1", "s2", "s3"])

        _run_tick()

        assert len(rec.calls) == 2
        deferred = _deferred_keys(path, {k for _, k in rec.calls})
        assert len(deferred) == 1
        story = _read_stories(path)[deferred[0]]
        assert story["status"] == "todo"
        assert story["dispatch_attempts"] == 0

    def test_case4_zero_cap_means_no_cap_all_three_dispatch_in_one_tick(
        self, monkeypatch, tmp_path
    ):
        _pin_tick_environment(monkeypatch)
        _set_cap(monkeypatch, 0)
        rec, path = _seed_tick(monkeypatch, tmp_path, ["s1", "s2", "s3"])

        _run_tick()

        assert sorted(k for _, k in rec.calls) == ["s1", "s2", "s3"]
        assert len(_deferred_keys(path, {k for _, k in rec.calls})) == 0

    def test_case5_negative_cap_behaves_like_zero_no_cap(
        self, monkeypatch, tmp_path
    ):
        _pin_tick_environment(monkeypatch)
        _set_cap(monkeypatch, -1)
        rec, _path = _seed_tick(monkeypatch, tmp_path, ["s1", "s2", "s3"])

        _run_tick()

        assert sorted(k for _, k in rec.calls) == ["s1", "s2", "s3"]

    def test_case6_malformed_env_degrades_to_default_one_at_tick_level(
        self, monkeypatch, tmp_path
    ):
        cfg = _reload_config_with(monkeypatch, "abc")  # must not raise
        assert cfg.PIPELINE_MAX_DISPATCH_PER_TICK == 1
        _pin_tick_environment(monkeypatch)
        _set_cap(monkeypatch, cfg.PIPELINE_MAX_DISPATCH_PER_TICK)
        rec, _path = _seed_tick(monkeypatch, tmp_path, ["s1", "s2", "s3"])

        _run_tick()

        assert len(rec.calls) == 1

    def test_case7_zero_ready_stories_tick_succeeds_with_zero_dispatches(
        self, monkeypatch, tmp_path
    ):
        _pin_tick_environment(monkeypatch)
        _set_cap(monkeypatch, 1)
        rec, _path = _seed_tick(monkeypatch, tmp_path, [])

        result = _run_tick()

        assert result["ok"] is True
        assert rec.calls == []

    def test_case8_failed_dispatch_does_not_consume_a_cap_slot(
        self, monkeypatch, tmp_path
    ):
        _pin_tick_environment(monkeypatch)
        _set_cap(monkeypatch, 1)
        rec, _path = _seed_tick(
            monkeypatch, tmp_path, ["s1", "s2"], fail_first=1
        )

        _run_tick()

        # The failed first dispatch must not consume the cap slot: the second
        # ready story is still attempted within the same tick.
        assert len(rec.calls) == 2
        assert sorted(k for _, k in rec.calls) == ["s1", "s2"]

    def test_deferred_interrupted_story_keeps_status_and_attempt_budget(
        self, monkeypatch, tmp_path
    ):
        _pin_tick_environment(monkeypatch)
        _set_cap(monkeypatch, 1)
        rec, path = _seed_tick(
            monkeypatch, tmp_path, ["s1", "s2"], statuses={"s2": "interrupted"}
        )

        _run_tick()

        assert len(rec.calls) == 1
        deferred = _deferred_keys(path, {k for _, k in rec.calls})
        assert len(deferred) == 1
        story = _read_stories(path)[deferred[0]]
        assert story["status"] == "interrupted"
        assert story["dispatch_attempts"] == 0
