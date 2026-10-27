"""Tests for the append-only, hash-chained plan audit stream (AUD-01).

One file per plan, ``<PLAN_DIR>/<plan>.audit.jsonl``: a JSON object per line,
each carrying the hash of the record before it. These tests pin the record
shape, the ``seq``/``prev_hash`` linkage, tamper detection, late binding of
``pipeline.persistence.PLAN_DIR``, the flock-serialized concurrent append, and
the never-raise contract of ``record_audit``.

What the chain does NOT detect is pinned here too, by omission rather than by
test: truncation of the tail and a full rewrite that recomputes every
downstream hash both verify intact. A chain with no external anchor cannot
prove the newest records were not removed, so the head hash also has to be
shipped somewhere the pipeline cannot rewrite.
"""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.identity import Principal
from pipeline import audit as audit_mod
from pipeline import persistence
from pipeline.actor import bind_correlation_id, bind_principal


@pytest.fixture()
def plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the audit stream at a temp plan directory for one test.

    ``pipeline.audit`` reads ``persistence.PLAN_DIR`` at call time, so patching
    the module attribute redirects this stream without the module capturing
    the operator's real plan directory at import.
    """
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    return tmp_path


# --------------------------------------------------------------------------
# Helpers: everything that writes, rewrites, deletes, or walks lines lives
# here so the test bodies stay one assertion each.
# --------------------------------------------------------------------------


def _canonical(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)


def _digest(record: dict) -> str:
    """sha256 of the record's canonical JSON with the record's own hash dropped."""
    payload = {key: value for key, value in record.items() if key != "hash"}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _stream(plan_dir: Path, plan_name: str = "p") -> Path:
    return plan_dir / f"{plan_name}.audit.jsonl"


def _write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n")


def _rewrite_line(path: Path, index: int, mutate) -> None:
    """Rewrite one line in place, leaving the record's own hash untouched."""
    lines = path.read_text().splitlines()
    record = json.loads(lines[index])
    mutate(record)
    lines[index] = _canonical(record)
    _write_lines(path, lines)


def _rehash_line(path: Path, index: int) -> None:
    """Recompute one line's own hash after an edit, so only linkage can break."""
    lines = path.read_text().splitlines()
    record = json.loads(lines[index])
    record["hash"] = _digest(record)
    lines[index] = _canonical(record)
    _write_lines(path, lines)


def _delete_line(path: Path, index: int) -> None:
    lines = path.read_text().splitlines()
    del lines[index]
    _write_lines(path, lines)


def _replace_line(path: Path, index: int, text: str) -> None:
    lines = path.read_text().splitlines()
    lines[index] = text
    _write_lines(path, lines)


def _append_blank_line(path: Path) -> None:
    path.write_text(path.read_text() + "\n")


def _set_outcome(record: dict) -> None:
    record["outcome"] = "denied"


def _clear_prev_hash(record: dict) -> None:
    record["prev_hash"] = ""


def _append_three_then_blank_line(plan_name: str, plan_dir: Path) -> None:
    for outcome in ("ok", "ok", "ok"):
        audit_mod.append_audit(plan_name, action="merge", outcome=outcome)
    _append_blank_line(_stream(plan_dir, plan_name))


def _concurrent_sequences(plan_name: str) -> list[int]:
    """Two threads append at the same moment; return the sorted ``seq`` values.

    An exception raised inside a worker propagates out of ``Future.result()``,
    so a failed append fails the test instead of vanishing into a thread.
    """
    barrier = threading.Barrier(2)

    def worker() -> int:
        barrier.wait()
        return int(audit_mod.append_audit(plan_name, action="merge", outcome="ok")["seq"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        return sorted(future.result() for future in futures)


def _raiser(*args, **kwargs) -> dict:
    raise OSError("disk full")


# --------------------------------------------------------------------------
# Record shape and linkage
# --------------------------------------------------------------------------


def test_first_record_starts_the_chain(plan_dir: Path) -> None:
    record = audit_mod.append_audit("p", action="merge", outcome="ok")

    assert (record["seq"], record["prev_hash"], record["hash"]) == (0, "", _digest(record))


def test_second_record_links_to_the_first(plan_dir: Path) -> None:
    first = audit_mod.append_audit("p", action="merge", outcome="ok")
    second = audit_mod.append_audit("p", action="merge", outcome="ok")

    assert (second["seq"], second["prev_hash"]) == (1, first["hash"])


def test_three_appends_verify_intact(plan_dir: Path) -> None:
    _append_three_then_blank_line("p", plan_dir)

    assert audit_mod.verify_audit_chain("p") is None


# --------------------------------------------------------------------------
# Tamper detection: rewriting, reordering, deleting in the middle
# --------------------------------------------------------------------------


def test_rewritten_outcome_is_reported(plan_dir: Path) -> None:
    _append_three_then_blank_line("p", plan_dir)
    _rewrite_line(_stream(plan_dir), 1, _set_outcome)

    assert audit_mod.verify_audit_chain("p") == 1


def test_broken_prev_hash_link_is_reported(plan_dir: Path) -> None:
    _append_three_then_blank_line("p", plan_dir)
    _rewrite_line(_stream(plan_dir), 1, _clear_prev_hash)
    _rehash_line(_stream(plan_dir), 1)

    assert audit_mod.verify_audit_chain("p") == 1


def test_deleted_middle_record_is_reported(plan_dir: Path) -> None:
    _append_three_then_blank_line("p", plan_dir)
    _delete_line(_stream(plan_dir), 1)

    assert audit_mod.verify_audit_chain("p") == 1


def test_unparseable_line_is_reported(plan_dir: Path) -> None:
    _append_three_then_blank_line("p", plan_dir)
    _replace_line(_stream(plan_dir), 1, "not json")

    assert audit_mod.verify_audit_chain("p") == 1


def test_missing_stream_verifies_intact(plan_dir: Path) -> None:
    assert audit_mod.verify_audit_chain("p") is None


# --------------------------------------------------------------------------
# Late binding of PLAN_DIR
# --------------------------------------------------------------------------


def test_audit_path_follows_plan_dir_rebinding(
    plan_dir: Path, tmp_path_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path_factory.mktemp("rebound")
    monkeypatch.setattr(persistence, "PLAN_DIR", other)

    assert audit_mod.audit_path("p").parent == other


# --------------------------------------------------------------------------
# Actor and correlation id come from the request context
# --------------------------------------------------------------------------


def test_record_carries_bound_actor_and_correlation_id(plan_dir: Path) -> None:
    principal = Principal(subject="alice", roles={"approver"})
    with bind_principal(principal), bind_correlation_id("req-4"):
        record = audit_mod.append_audit("p", action="merge", outcome="ok")

    assert (record["actor"], record["correlation_id"]) == ("alice", "req-4")


# --------------------------------------------------------------------------
# Concurrency: the flock on the stream serializes writers
# --------------------------------------------------------------------------


def test_concurrent_appends_serialize(plan_dir: Path) -> None:
    assert (
        _concurrent_sequences("p"),
        audit_mod.verify_audit_chain("p"),
    ) == ([0, 1], None)


# --------------------------------------------------------------------------
# record_audit must never change the caller's decision
# --------------------------------------------------------------------------


def test_record_audit_swallows_append_failure(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audit_mod, "append_audit", _raiser)

    assert audit_mod.record_audit("p", action="merge", outcome="ok") is None
