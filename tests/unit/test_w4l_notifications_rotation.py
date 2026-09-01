"""TDD spec: retention/rotation policy for per-plan notification sinks.

Covers the plan doc's last Logging bullet: the per-plan notification sinks --
the structured ``<plan>.notifications.jsonl`` written by
``pipeline.persistence._write_notification_record`` (also used by the sink)
and the free-text ``<plan>.notifications.log`` written by
``pipeline.notification_sinks.file_log_sink`` -- must rotate under a shared
byte-cap + keep-N policy instead of appending without bound:

* ``persistence.NOTIFICATIONS_MAX_BYTES`` (env
  ``PIPELINE_NOTIFICATIONS_MAX_BYTES``, default ``2 * 1024 * 1024``): before
  appending, rotate the active file when its size EXCEEDS this value.
  ``<= 0`` disables rotation entirely (append-only, current behavior).
* ``persistence.NOTIFICATIONS_KEEP_N`` (env ``PIPELINE_NOTIFICATIONS_KEEP``,
  default ``3``): numbered generations beyond this are deleted.
* A single shared helper (``persistence._rotate_if_needed(path, max_bytes,
  keep)``) must serve BOTH writers so the two writers cannot drift.
* Rotation is best-effort: an OSError during rotation must fall back to a
  plain append and must never propagate out of ``_write_notification_record``
  or the sink.

These tests are expected to FAIL (AttributeError on the missing constants /
helper, then assertion failures) until the implementation lands.  PLAN_DIR is
patched through the existing Option B seam used by the other persistence
tests (``monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)``) and the
policy constants are monkeypatched per test (so the implementation must read
them at call time, not bake them into a default argument).
"""

import importlib
import inspect
import json
import os
from pathlib import Path

import pytest

from pipeline import notification_sinks, persistence

PLAN = "rotplan"
JSONL = f"{PLAN}.notifications.jsonl"
LOG = f"{PLAN}.notifications.log"


# --------------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point persistence.PLAN_DIR (the Option B seam) at a tmp dir."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    return tmp_path


def _record(n, pad=50):
    """A small deterministic record; line length grows with ``pad``."""
    return {"n": n, "message": f"m{n}-" + "x" * pad}


def _sink_event(n, pad=50):
    """A bus notification event whose free-text line is deterministic."""
    return {
        "type": "notification",
        "plan": PLAN,
        "ts": f"2025-01-01T00:00:{n:02d}+00:00",
        "payload": {
            "message": f"m{n}-" + "x" * pad,
            "severity": "info",
            "event": "ev",
            "dedup_key": f"dk{n}",
        },
    }


def _jsonl_path(plan_dir):
    return plan_dir / JSONL


def _log_path(plan_dir):
    return plan_dir / LOG


def _numbered(plan_dir, base_name):
    """Numbered generations (``<base>.1``, ``<base>.2``, ...) sorted by number."""
    return sorted(plan_dir.glob(f"{base_name}.*"), key=lambda p: p.name)


def _read_jsonl(path):
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _log_index(line):
    """Parse the record index out of a free-text line ``"{ts} m<n>-xxx"``."""
    return int(line.split(" ", 1)[1].split("-", 1)[0][1:])


# --------------------------------------------------------------------------- #
# Policy constants exist and read their env vars
# --------------------------------------------------------------------------- #


def test_policy_constants_exist_as_module_level_ints():
    assert isinstance(persistence.NOTIFICATIONS_MAX_BYTES, int), (
        "persistence must define NOTIFICATIONS_MAX_BYTES (env "
        "PIPELINE_NOTIFICATIONS_MAX_BYTES, default 2 MiB)"
    )
    assert isinstance(persistence.NOTIFICATIONS_KEEP_N, int), (
        "persistence must define NOTIFICATIONS_KEEP_N (env "
        "PIPELINE_NOTIFICATIONS_KEEP, default 3)"
    )


def test_policy_constants_read_configured_env(monkeypatch):
    """The constants must be env-driven: defaults when unset, values when set."""
    monkeypatch.delenv("PIPELINE_NOTIFICATIONS_MAX_BYTES", raising=False)
    monkeypatch.delenv("PIPELINE_NOTIFICATIONS_KEEP", raising=False)
    importlib.reload(persistence)
    try:
        assert persistence.NOTIFICATIONS_MAX_BYTES == 2 * 1024 * 1024
        assert persistence.NOTIFICATIONS_KEEP_N == 3
        monkeypatch.setenv("PIPELINE_NOTIFICATIONS_MAX_BYTES", "4096")
        monkeypatch.setenv("PIPELINE_NOTIFICATIONS_KEEP", "5")
        importlib.reload(persistence)
        assert persistence.NOTIFICATIONS_MAX_BYTES == 4096
        assert persistence.NOTIFICATIONS_KEEP_N == 5
    finally:
        monkeypatch.delenv("PIPELINE_NOTIFICATIONS_MAX_BYTES", raising=False)
        monkeypatch.delenv("PIPELINE_NOTIFICATIONS_KEEP", raising=False)
        importlib.reload(persistence)


# --------------------------------------------------------------------------- #
# Scenario 1: writing past the byte cap rotates the JSONL
# --------------------------------------------------------------------------- #


def test_write_past_byte_cap_rotates_jsonl(plan_dir, monkeypatch):
    monkeypatch.setattr(persistence, "NOTIFICATIONS_MAX_BYTES", 200)
    monkeypatch.setattr(persistence, "NOTIFICATIONS_KEEP_N", 3)
    for i in range(12):
        persistence._write_notification_record(PLAN, _record(i))  # must not raise

    gens = _numbered(plan_dir, JSONL)
    assert [g.name for g in gens] == [f"{JSONL}.1", f"{JSONL}.2", f"{JSONL}.3"], (
        "writing past the byte cap must rotate: .jsonl.1 must exist and older "
        "generations must shift (.jsonl.1 -> .jsonl.2 -> ...)"
    )
    # Oldest -> newest: gen3, gen2, gen1, active.
    ordered = (
        _read_jsonl(gens[2])
        + _read_jsonl(gens[1])
        + _read_jsonl(gens[0])
        + _read_jsonl(_jsonl_path(plan_dir))
    )
    assert [r["n"] for r in ordered] == list(range(12)), (
        "rotation must preserve every record exactly once, in order"
    )
    gen1 = _read_jsonl(gens[0])
    active = _read_jsonl(_jsonl_path(plan_dir))
    assert gen1, ".jsonl.1 must contain the older records"
    assert active, "the active .jsonl must contain the newer records"
    assert max(r["n"] for r in gen1) < min(r["n"] for r in active), (
        "the active .jsonl must contain only records written after the rotation"
    )


# --------------------------------------------------------------------------- #
# Scenario 2: rotation honors KEEP (old generations are deleted)
# --------------------------------------------------------------------------- #


def test_rotation_honors_keep(plan_dir, monkeypatch):
    monkeypatch.setattr(persistence, "NOTIFICATIONS_MAX_BYTES", 200)
    monkeypatch.setattr(persistence, "NOTIFICATIONS_KEEP_N", 1)
    for batch in range(4):  # 4 batches -> >= 3 rotations
        for i in range(3):
            persistence._write_notification_record(PLAN, _record(batch * 10 + i))

    gen1 = plan_dir / f"{JSONL}.1"
    assert gen1.exists(), "rotation must produce a .jsonl.1 generation"
    for stale in (2, 3, 4):
        assert not (plan_dir / f"{JSONL}.{stale}").exists(), (
            f"generation {stale} must be deleted: KEEP=1 retains only generation 1"
        )
    survivors = _read_jsonl(gen1) + _read_jsonl(_jsonl_path(plan_dir))
    ns = {r["n"] for r in survivors}
    assert ns, "the retained generation must not be empty"
    assert ns.isdisjoint({0, 1, 2, 10, 11, 12}), (
        "records from deleted generations must be gone, not retained"
    )
    assert ns <= {20, 21, 22, 30, 31, 32}, (
        "only the most recent generations may survive a KEEP=1 rotation"
    )
    assert len(survivors) < 12, "retention must actually bound the stored data"


# --------------------------------------------------------------------------- #
# Scenario 3: disabled rotation (cap <= 0) -> append-only, file grows
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cap", [0, -1, -999])
def test_rotation_disabled_when_cap_non_positive(plan_dir, monkeypatch, cap):
    monkeypatch.setattr(persistence, "NOTIFICATIONS_MAX_BYTES", cap)
    monkeypatch.setattr(persistence, "NOTIFICATIONS_KEEP_N", 3)
    for i in range(12):
        persistence._write_notification_record(PLAN, _record(i))
    assert _numbered(plan_dir, JSONL) == [], (
        f"cap={cap} disables rotation: no numbered generation may ever appear"
    )
    assert [r["n"] for r in _read_jsonl(_jsonl_path(plan_dir))] == list(range(12)), (
        "disabled rotation must be append-only: every record stays in the active file"
    )

    # The sink's two destinations under the same disabled policy.
    for i in range(6):
        notification_sinks.file_log_sink(_sink_event(i, pad=5))
    assert _numbered(plan_dir, LOG) == [], ".log must not rotate while disabled"
    assert _numbered(plan_dir, JSONL) == [], ".jsonl must not rotate while disabled"
    assert len(_log_path(plan_dir).read_text().splitlines()) == 6
    assert len(_read_jsonl(_jsonl_path(plan_dir))) == 18


# --------------------------------------------------------------------------- #
# Scenario 4: OSError during rotation must not break the record write
# --------------------------------------------------------------------------- #


def test_rotation_oserror_falls_back_to_plain_append(plan_dir, monkeypatch):
    monkeypatch.setattr(persistence, "NOTIFICATIONS_MAX_BYTES", 100)
    monkeypatch.setattr(persistence, "NOTIFICATIONS_KEEP_N", 3)
    first = _record(0, pad=100)  # single line well over the 100-byte cap
    persistence._write_notification_record(PLAN, first)

    attempted = []

    def boom(*args, **kwargs):
        attempted.append(args or kwargs)
        raise OSError("E_ROTATE_FAILED simulated rotation failure")

    monkeypatch.setattr(os, "replace", boom)
    monkeypatch.setattr(os, "rename", boom)

    second = _record(1, pad=10)
    persistence._write_notification_record(PLAN, second)  # must not raise
    assert attempted, "rotation must have been attempted for the oversize file"
    lines = _read_jsonl(_jsonl_path(plan_dir))
    assert lines == [first, second], (
        "on rotation failure the record must still be appended (fallback), "
        "with the earlier data intact and no exception propagated"
    )


def test_sink_survives_rotation_oserror(plan_dir, monkeypatch):
    monkeypatch.setattr(persistence, "NOTIFICATIONS_MAX_BYTES", 100)
    monkeypatch.setattr(persistence, "NOTIFICATIONS_KEEP_N", 3)
    first = _record(0, pad=100)
    persistence._write_notification_record(PLAN, first)
    _log_path(plan_dir).write_text("t0 " + "x" * 100 + "\n", encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("E_ROTATE_FAILED simulated rotation failure")

    monkeypatch.setattr(os, "replace", boom)
    monkeypatch.setattr(os, "rename", boom)

    evt = _sink_event(7, pad=10)
    notification_sinks.file_log_sink(evt)  # must not raise
    assert "m7-" in _log_path(plan_dir).read_text(), (
        "the free-text .log write must fall back to a plain append when "
        "rotation fails"
    )
    records = _read_jsonl(_jsonl_path(plan_dir))
    assert len(records) == 2, "the JSONL record must still be written (fallback)"
    assert records[-1]["message"] == "m7-" + "x" * 10


# --------------------------------------------------------------------------- #
# Scenario 5: the free-text .log rotates under the same policy
# --------------------------------------------------------------------------- #


def test_free_text_log_rotates_under_same_policy(plan_dir, monkeypatch):
    monkeypatch.setattr(persistence, "NOTIFICATIONS_MAX_BYTES", 200)
    monkeypatch.setattr(persistence, "NOTIFICATIONS_KEEP_N", 3)
    for i in range(12):
        notification_sinks.file_log_sink(_sink_event(i))

    gens = _numbered(plan_dir, LOG)
    assert gens, "the free-text .log must rotate under the same policy"
    assert gens[0].name == f"{LOG}.1"
    assert not (plan_dir / f"{LOG}.4").exists(), "KEEP=3 bounds the .log generations"

    # Oldest generation first: .3, .2, .1, then the active file.
    ordered_lines = []
    for gen in reversed(gens):
        ordered_lines.extend(gen.read_text(encoding="utf-8").splitlines())
    ordered_lines.extend(_log_path(plan_dir).read_text(encoding="utf-8").splitlines())
    ns = [_log_index(line) for line in ordered_lines]
    assert sorted(ns) == list(range(12)), (
        ".log rotation must preserve every line exactly once"
    )
    assert ns == sorted(ns), ".log rotation must preserve chronological order"

    # The same sink call also persists the structured record: it rotates too.
    assert _numbered(plan_dir, JSONL), (
        "the JSONL side of the sink must rotate under the same policy"
    )


# --------------------------------------------------------------------------- #
# Scenario 6: rotation does not alter the JSONL record content
# --------------------------------------------------------------------------- #


def test_rotation_preserves_record_shape(plan_dir, monkeypatch):
    monkeypatch.setattr(persistence, "NOTIFICATIONS_MAX_BYTES", 50)
    rec1 = persistence._notification_record(
        PLAN,
        "hello world",
        "S1",
        "warning",
        "ci_pending_stalled",
        "dk-1",
        "2025-01-01T00:00:00+00:00",
        correlation_id="corr-1",
        attempt=2,
        role="implementer",
        provider="openai",
        model="gpt-4o",
    )
    rec2 = persistence._notification_record(
        PLAN, "second notice", "S2", "error", "review_blocked", "dk-2",
        "2025-01-01T00:00:01+00:00",
    )
    persistence._write_notification_record(PLAN, rec1)
    persistence._write_notification_record(PLAN, rec2)  # rec1's line > 50 bytes

    gen1 = plan_dir / f"{JSONL}.1"
    assert gen1.exists(), "the oversize first record must have been rotated"
    old_lines = gen1.read_text(encoding="utf-8").splitlines()
    new_lines = _jsonl_path(plan_dir).read_text(encoding="utf-8").splitlines()
    assert len(old_lines) == 1 and len(new_lines) == 1
    assert json.loads(old_lines[0]) == rec1, (
        "rotation must not alter the JSONL record content (including the "
        "optional correlation/context kwargs)"
    )
    assert json.loads(new_lines[0]) == rec2


# --------------------------------------------------------------------------- #
# The single shared rotation helper (no drift between the two writers)
# --------------------------------------------------------------------------- #


def test_rotation_helper_exists_with_policy_signature():
    rotate = getattr(persistence, "_rotate_if_needed", None)
    assert callable(rotate), (
        "persistence must expose ONE shared rotation helper "
        "_rotate_if_needed(path, max_bytes, keep) used by both writers"
    )
    inspect.signature(rotate).bind(Path("whatever"), 1024, 3)


def test_rotation_helper_shifts_and_deletes_generations(plan_dir):
    rotate = persistence._rotate_if_needed
    target = plan_dir / "solo.bin"

    # Missing file: a no-op that never raises.
    rotate(target, 100, 3)
    assert not target.exists()
    assert _numbered(plan_dir, "solo.bin") == []

    # Under the cap: untouched.
    target.write_text("a" * 40, encoding="utf-8")
    rotate(target, 100, 3)
    assert target.read_text(encoding="utf-8") == "a" * 40
    assert _numbered(plan_dir, "solo.bin") == []

    # Over the cap: renamed to generation 1.
    target.write_text("b" * 120, encoding="utf-8")
    rotate(target, 100, 3)
    gen1 = plan_dir / "solo.bin.1"
    assert gen1.exists() and gen1.read_text(encoding="utf-8") == "b" * 120
    assert not target.exists() or target.stat().st_size == 0

    # Existing generations shift up: .1 -> .2, active -> .1.
    target.write_text("c" * 120, encoding="utf-8")
    gen1.write_text("gen-1", encoding="utf-8")
    rotate(target, 100, 3)
    assert gen1.read_text(encoding="utf-8") == "c" * 120
    assert (plan_dir / "solo.bin.2").read_text(encoding="utf-8") == "gen-1"

    # A fourth rotation creates gen3; a fifth pushes gen3 -> gen4 which is
    # then deleted because keep=3.
    target.write_text("d" * 120, encoding="utf-8")
    rotate(target, 100, 3)
    assert (plan_dir / "solo.bin.3").read_text(encoding="utf-8") == "gen-1"
    target.write_text("e" * 120, encoding="utf-8")
    rotate(target, 100, 3)
    assert not (plan_dir / "solo.bin.4").exists(), (
        "generations beyond KEEP must be deleted"
    )
    assert (plan_dir / "solo.bin.3").read_text(encoding="utf-8") == "c" * 120
    assert (plan_dir / "solo.bin.2").read_text(encoding="utf-8") == "d" * 120
    assert gen1.read_text(encoding="utf-8") == "e" * 120


def test_both_writers_route_through_shared_helper(plan_dir, monkeypatch):
    """Both the JSONL writer and the sink's .log writer must use ONE helper."""
    real_rotate = persistence._rotate_if_needed
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_rotate(*args, **kwargs)

    monkeypatch.setattr(persistence, "_rotate_if_needed", spy)
    monkeypatch.setattr(persistence, "NOTIFICATIONS_MAX_BYTES", 50)
    monkeypatch.setattr(persistence, "NOTIFICATIONS_KEEP_N", 2)

    # Oversize the JSONL, then write: the writer must consult the helper.
    persistence._write_notification_record(PLAN, _record(0, pad=80))
    calls.clear()
    persistence._write_notification_record(PLAN, _record(1, pad=80))
    assert calls, (
        "_write_notification_record must consult the shared rotation helper "
        "before appending"
    )

    # Oversize the free-text .log, then drive the sink: same helper, no drift.
    calls.clear()
    _log_path(plan_dir).write_text("t0 " + "x" * 200 + "\n", encoding="utf-8")
    notification_sinks.file_log_sink(_sink_event(1, pad=10))

    def _call_path(call):
        args, kwargs = call
        if args:
            return Path(args[0])
        return Path(kwargs.get("path") or "")

    log_calls = [c for c in calls if _call_path(c).name.endswith(".notifications.log")]
    assert log_calls, (
        "file_log_sink's .log write must use the same shared rotation helper "
        "(the two writers must not drift)"
    )


# --------------------------------------------------------------------------- #
# Boundary: a file exactly at the cap must NOT rotate (policy: "exceeds")
# --------------------------------------------------------------------------- #


def test_no_rotation_when_size_exactly_at_cap(plan_dir, monkeypatch):
    monkeypatch.setattr(persistence, "NOTIFICATIONS_MAX_BYTES", 100)
    monkeypatch.setattr(persistence, "NOTIFICATIONS_KEEP_N", 3)
    rec = {"n": 0, "message": ""}
    rec["message"] = "x" * (100 - (len(json.dumps(rec)) + 1))
    line = json.dumps(rec) + "\n"
    assert len(line.encode("utf-8")) == 100, "test arithmetic guard"
    persistence._write_notification_record(PLAN, rec)
    persistence._write_notification_record(PLAN, {"n": 1, "message": "y"})
    assert _numbered(plan_dir, JSONL) == [], (
        "a file whose size equals the cap must not rotate; rotation happens "
        "only when the size EXCEEDS the cap"
    )


# --------------------------------------------------------------------------- #
# Guard: the W4L-01 optional-kwargs additions to _notification_record survive
# --------------------------------------------------------------------------- #


def test_notification_record_optional_kwargs_intact():
    rec = persistence._notification_record(
        PLAN,
        "msg",
        "S1",
        "info",
        "ev",
        "dk",
        "2025-01-01T00:00:00+00:00",
        correlation_id="c",
        attempt=1,
        role="r",
        provider="p",
        model="m",
    )
    for key, expected in (
        ("correlation_id", "c"),
        ("attempt", 1),
        ("role", "r"),
        ("provider", "p"),
        ("model", "m"),
    ):
        assert rec[key] == expected
    bare = persistence._notification_record(
        PLAN, "m", None, "info", None, None, "ts"
    )
    assert not {"correlation_id", "attempt", "role", "provider", "model"} & set(bare), (
        "legacy record shape must be unchanged when the optional kwargs are unset"
    )
