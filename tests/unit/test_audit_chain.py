"""Tests for the pure audit hash-chain module (ADR 0012, decision 10).

The hash chain has to be one algorithm in one place: every store -- memory,
file, SQLite -- must hash the same bytes or a chain written by one cannot be
verified by another.  These tests pin the golden digests of a known record,
the byte-for-byte agreement with the module the algorithm came from, the
detection behaviour of ``verify_entries`` and the linking rules of ``link``.
"""

from __future__ import annotations

import datetime

from pipeline import audit as audit_mod
from pipeline import audit_chain

#: The record the golden digests were computed from.
GOLDEN_RECORD = {
    "seq": 0,
    "prev_hash": "",
    "ts": "2026-01-01T00:00:00+00:00",
    "actor": "tester",
    "principal_type": "human",
    "client_id": "",
    "correlation_id": "cid-1",
    "action": "story.merge",
    "outcome": "allowed",
    "target": {"plan": "p", "story": "S1"},
    "detail": {"sha": "abc"},
}

#: The digest of :data:`GOLDEN_RECORD`.
GOLDEN_HASH = "7073393fd5ecfa1001567139db7345a6afa029e36aeba651bfa9d072e7436395"

#: The digest of the same record one step down the chain.
GOLDEN_SECOND_HASH = "f913fda5510c978a8c57bd0ef7a08c95a9df270ac10ea758545cdf08eed8aaa2"


def _chain(count: int) -> list[dict]:
    """Return *count* linked records, built with the module under test."""
    records: list[dict] = []
    previous = None
    for index in range(count):
        record = audit_chain.link(
            previous, {"action": f"a{index}", "outcome": "allowed"}
        )
        records.append(record)
        previous = record
    return records


def _link_error(body: dict) -> ValueError:
    """Return the error ``link`` raises for *body*, failing when it raises none."""
    try:
        audit_chain.link(None, body)
    except ValueError as error:
        return error
    raise AssertionError("link accepted a body that already carries chain fields")


# --------------------------------------------------------------------------
# Golden vectors
# --------------------------------------------------------------------------


def test_the_golden_record_hashes_to_the_pinned_digest():
    assert audit_chain.record_hash(GOLDEN_RECORD) == GOLDEN_HASH


def test_the_second_golden_record_hashes_to_the_pinned_digest():
    second = dict(GOLDEN_RECORD, seq=1, prev_hash=GOLDEN_HASH, outcome="denied")
    assert audit_chain.record_hash(second) == GOLDEN_SECOND_HASH


# --------------------------------------------------------------------------
# canonical / record_hash
# --------------------------------------------------------------------------


def test_canonical_is_byte_identical_to_the_audit_module():
    assert audit_chain.canonical(GOLDEN_RECORD) == audit_mod._canonical(GOLDEN_RECORD)


def test_canonical_renders_a_datetime_the_same_way_as_the_audit_module():
    record = {"ts": datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)}
    assert audit_chain.canonical(record) == audit_mod._canonical(record)


def test_record_hash_ignores_an_existing_hash_field():
    with_hash = dict(GOLDEN_RECORD, hash="not-the-real-digest")
    assert audit_chain.record_hash(with_hash) == GOLDEN_HASH


def test_record_hash_changes_when_any_other_field_changes():
    edited = dict(GOLDEN_RECORD, detail={"sha": "changed"})
    assert audit_chain.record_hash(edited) != GOLDEN_HASH


# --------------------------------------------------------------------------
# verify_entries
# --------------------------------------------------------------------------


def test_verify_entries_accepts_an_empty_chain():
    assert audit_chain.verify_entries([]) is None


def test_verify_entries_accepts_one_valid_record():
    assert audit_chain.verify_entries(_chain(1)) is None


def test_verify_entries_accepts_a_valid_chain():
    assert audit_chain.verify_entries(_chain(5)) is None


def test_verify_entries_reports_a_missing_entry():
    entries = _chain(3)
    entries[2] = None
    assert audit_chain.verify_entries(entries) == 2


def test_verify_entries_reports_an_entry_that_is_not_an_object():
    entries = _chain(2)
    entries[1] = "not a record"
    assert audit_chain.verify_entries(entries) == 1


def test_verify_entries_reports_a_gap_in_the_sequence():
    entries = _chain(3)
    entries[2] = dict(entries[2], seq=3)
    assert audit_chain.verify_entries(entries) == 2


def test_verify_entries_reports_a_wrong_prev_hash():
    entries = _chain(3)
    entries[1] = dict(entries[1], prev_hash="0" * 64)
    assert audit_chain.verify_entries(entries) == 1


def test_verify_entries_reports_an_edited_field():
    entries = _chain(3)
    entries[1] = dict(entries[1], detail={"sha": "tampered"})
    assert audit_chain.verify_entries(entries) == 1


def test_verify_entries_reports_a_first_record_that_does_not_start_at_zero():
    entries = _chain(2)
    entries[0] = dict(entries[0], seq=1)
    assert audit_chain.verify_entries(entries) == 0


# --------------------------------------------------------------------------
# link
# --------------------------------------------------------------------------


def test_link_starts_a_chain_at_seq_zero():
    record = audit_chain.link(None, {"action": "a", "outcome": "allowed"})
    assert (record["seq"], record["prev_hash"]) == (0, "")


def test_link_carries_the_body_into_the_record():
    body = {"action": "a", "outcome": "allowed", "detail": {"k": 1}}
    record = audit_chain.link(None, body)
    assert record["action"] == "a"
    assert record["outcome"] == "allowed"
    assert record["detail"] == {"k": 1}


def test_link_continues_an_existing_chain():
    previous = audit_chain.link(None, {"action": "a", "outcome": "allowed"})
    record = audit_chain.link(previous, {"action": "b", "outcome": "allowed"})
    assert record["seq"] == previous["seq"] + 1
    assert record["prev_hash"] == previous["hash"]


def test_link_hashes_the_record_it_builds():
    record = audit_chain.link(None, {"action": "a", "outcome": "allowed"})
    assert record["hash"] == audit_chain.record_hash(record)


def test_link_refuses_a_body_that_carries_a_seq():
    error = _link_error({"seq": 99, "action": "a"})
    assert "seq" in str(error)
    assert "99" not in str(error)


def test_link_refuses_a_body_that_carries_a_prev_hash():
    error = _link_error({"prev_hash": "abc123", "action": "a"})
    assert "prev_hash" in str(error)
    assert "abc123" not in str(error)


def test_link_refuses_a_body_that_carries_a_hash():
    error = _link_error({"hash": "deadbeef", "action": "a"})
    assert "hash" in str(error)
    assert "deadbeef" not in str(error)


# --------------------------------------------------------------------------
# The module the algorithm came from keeps its names
# --------------------------------------------------------------------------


def test_the_audit_module_keeps_its_canonical_delegate():
    assert audit_mod._canonical(GOLDEN_RECORD) == audit_chain.canonical(GOLDEN_RECORD)


def test_the_audit_module_keeps_its_record_hash_delegate():
    assert audit_mod._record_hash(GOLDEN_RECORD) == audit_chain.record_hash(GOLDEN_RECORD)


def test_the_audit_module_keeps_its_verify_chain_at_delegate(tmp_path):
    records = _chain(3)
    path = tmp_path / "chain.jsonl"
    path.write_text("\n".join(audit_chain.canonical(record) for record in records) + "\n")
    assert audit_mod._verify_chain_at(path) is None


def test_the_audit_module_delegate_reports_a_tampered_record(tmp_path):
    records = _chain(3)
    records[1] = dict(records[1], detail={"sha": "tampered"})
    path = tmp_path / "chain.jsonl"
    path.write_text("\n".join(audit_chain.canonical(record) for record in records) + "\n")
    assert audit_mod._verify_chain_at(path) == 1
