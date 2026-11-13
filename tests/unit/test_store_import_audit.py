"""The one-time import of file-store audit chains into the SQLite audit table.

ADR 0012 decision 10: the hash chain is unchanged by the move, so a chain is
copied row for row and never re-appended.  A re-append would re-link every
record and change every hash, and the file anchor's recorded heads would no
longer match.

The fixtures are b4's committed legacy streams, copied into ``tmp_path`` so the
tests can damage them without touching the originals.  The source files are
never written: every test that imports reads them back afterwards.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

import pytest

from pipeline import audit_chain
from pipeline.audit import HOST_AUDIT_FILE
from pipeline.audit_anchor import PIPELINE_AUDIT_ANCHOR, FileAnchorSink
from pipeline.audit_file import SUFFIX, FileAuditLog
from pipeline.audit_log import HOST_CHAIN_ID
from pipeline.store_import_audit import import_audit_chains
from pipeline.store_records import DEFAULT_NAMESPACE, UnknownNamespaceError
from pipeline.store_sqlite import SqliteStore
from pipeline.store_sqlite_audit import AuditChainConflictError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "audit"
LEGACY_PLAN = "legacy-plan"
LEGACY_LINE_OF_ACTOR = '"actor":"legacy-fixture"'
PACKAGE = Path(__file__).resolve().parents[2] / "pipeline"
APP = Path(__file__).resolve().parents[2] / "app"
IMPORT_CHAIN_CALL = re.compile(r"\bimport_chain\b")


def _plans(tmp_path: Path) -> Path:
    """Copy both legacy fixtures into a fresh plans directory and return it."""
    plans = tmp_path / "plans"
    plans.mkdir()
    shutil.copy(FIXTURES / "legacy.audit.jsonl", plans / f"{LEGACY_PLAN}{SUFFIX}")
    shutil.copy(FIXTURES / "legacy.audit.host.jsonl", plans / HOST_AUDIT_FILE)
    return plans


def _source(plans: Path) -> FileAuditLog:
    return FileAuditLog(plans)


def _target(tmp_path: Path) -> tuple[SqliteStore, object]:
    store = SqliteStore(str(tmp_path / "store.db"))
    return store, store.audit_log()


def _statuses(report: list[dict]) -> dict[str, str]:
    return {entry["chain"]: entry["status"] for entry in report}


def _row_count(log, chain: str) -> int:
    return len(log.read_chain(DEFAULT_NAMESPACE, chain))


class _FixedSource:
    """A source of in-memory chains, for a chain the file layout cannot express."""

    def __init__(self, chains: dict[str, list[dict]]) -> None:
        self._chains = chains

    def list_chains(self, namespace):
        return list(self._chains)

    def read_chain(self, namespace, chain):
        return self._chains[chain]


def _anchor_of(source, chain: str, index: int) -> tuple[int, str]:
    """The ``(seq, hash)`` of the record at *index* of *chain*, as an anchor holds it."""
    record = source.read_chain(DEFAULT_NAMESPACE, chain)[index]
    return record["seq"], record["hash"]


def _configure_anchor(
    monkeypatch, tmp_path: Path, heads: dict[str, tuple[int, str]]
) -> None:
    """Point the anchor sink at a file that holds one anchor per chain in *heads*."""
    log = tmp_path / "anchors.jsonl"
    sink = FileAnchorSink(log)
    for chain, (seq, digest) in heads.items():
        sink.publish(DEFAULT_NAMESPACE, chain, seq, digest)
    monkeypatch.setenv(PIPELINE_AUDIT_ANCHOR, f"file:{log}")


def _chain(*bodies: dict) -> list[dict]:
    """Return a valid chain of *bodies*, linked the way the writer links them."""
    records: list[dict] = []
    for body in bodies:
        records.append(audit_chain.link(records[-1] if records else None, body))
    return records


# -- the import itself ------------------------------------------------------
def test_imports_the_plan_and_host_chains(tmp_path):
    store, target = _target(tmp_path)
    report = import_audit_chains(_source(_plans(tmp_path)), target)

    assert {
        entry["chain"]: (entry["status"], entry["records"]) for entry in report
    } == {
        LEGACY_PLAN: ("imported", 2),
        HOST_CHAIN_ID: ("imported", 1),
    }
    store.close()


def test_the_table_reads_back_the_file_records_and_verifies(tmp_path):
    plans = _plans(tmp_path)
    store, target = _target(tmp_path)
    source = _source(plans)
    import_audit_chains(source, target)

    for chain in (LEGACY_PLAN, HOST_CHAIN_ID):
        records = target.read_chain(DEFAULT_NAMESPACE, chain)
        assert records == source.read_chain(DEFAULT_NAMESPACE, chain)
        assert audit_chain.verify_entries(records) is None
    store.close()


def test_head_hashes_are_identical_to_the_file_heads(tmp_path):
    # A file anchor recorded before the import holds these heads, so equality
    # is what keeps it matching after the move.
    plans = _plans(tmp_path)
    store, target = _target(tmp_path)
    source = _source(plans)
    import_audit_chains(source, target)

    for chain in (LEGACY_PLAN, HOST_CHAIN_ID):
        assert target.head(DEFAULT_NAMESPACE, chain) == source.head(
            DEFAULT_NAMESPACE, chain
        )
    store.close()


def test_a_second_run_imports_nothing_and_writes_no_rows(tmp_path):
    plans = _plans(tmp_path)
    store, target = _target(tmp_path)
    import_audit_chains(_source(plans), target)
    before = {c: _row_count(target, c) for c in (LEGACY_PLAN, HOST_CHAIN_ID)}

    report = import_audit_chains(_source(plans), target)

    assert _statuses(report) == {
        LEGACY_PLAN: "already_present",
        HOST_CHAIN_ID: "already_present",
    }
    assert {c: _row_count(target, c) for c in before} == before
    store.close()


def test_a_dry_run_reports_the_same_and_writes_no_rows(tmp_path):
    plans = _plans(tmp_path)
    store, target = _target(tmp_path)

    dry = import_audit_chains(_source(plans), target, dry_run=True)

    assert _row_count(target, LEGACY_PLAN) == 0
    assert _row_count(target, HOST_CHAIN_ID) == 0
    real = import_audit_chains(_source(plans), target)
    assert dry == real
    store.close()


def test_an_empty_plans_directory_yields_no_chains(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    store, target = _target(tmp_path)

    assert import_audit_chains(_source(empty), target) == []
    store.close()


# -- refusals, per chain ----------------------------------------------------
def test_a_tampered_line_refuses_its_chain_and_the_sibling_still_imports(tmp_path):
    plans = _plans(tmp_path)
    stream = plans / f"{LEGACY_PLAN}{SUFFIX}"
    text = stream.read_text()
    assert LEGACY_LINE_OF_ACTOR in text
    stream.write_text(text.replace(LEGACY_LINE_OF_ACTOR, '"actor":"forged"', 1))
    store, target = _target(tmp_path)

    report = {e["chain"]: e for e in import_audit_chains(_source(plans), target)}

    assert report[LEGACY_PLAN]["status"] == "refused"
    assert report[LEGACY_PLAN]["reason"] == "unverifiable"
    assert report[LEGACY_PLAN]["seq"] == 0
    assert _row_count(target, LEGACY_PLAN) == 0
    assert report[HOST_CHAIN_ID]["status"] == "imported"
    assert _row_count(target, HOST_CHAIN_ID) == 1
    store.close()


def test_a_truncated_last_line_refuses_the_chain(tmp_path):
    plans = _plans(tmp_path)
    stream = plans / f"{LEGACY_PLAN}{SUFFIX}"
    lines = stream.read_text().splitlines()
    # The last line cut short, with no newline: what a crash mid-append leaves.
    stream.write_text(lines[0] + "\n" + lines[1][:40])
    store, target = _target(tmp_path)

    report = {e["chain"]: e for e in import_audit_chains(_source(plans), target)}

    assert report[LEGACY_PLAN]["status"] == "refused"
    assert report[LEGACY_PLAN]["reason"] == "unverifiable"
    assert report[LEGACY_PLAN]["seq"] == 1
    assert _row_count(target, LEGACY_PLAN) == 0
    store.close()


def test_a_differing_chain_already_in_the_table_is_refused_as_a_conflict(tmp_path):
    plans = _plans(tmp_path)
    store, target = _target(tmp_path)
    target.append(DEFAULT_NAMESPACE, LEGACY_PLAN, {"action": "written.elsewhere"})
    before = target.read_chain(DEFAULT_NAMESPACE, LEGACY_PLAN)

    report = {e["chain"]: e for e in import_audit_chains(_source(plans), target)}

    assert report[LEGACY_PLAN]["status"] == "refused"
    assert report[LEGACY_PLAN]["reason"] == "conflict"
    assert target.read_chain(DEFAULT_NAMESPACE, LEGACY_PLAN) == before
    assert report[HOST_CHAIN_ID]["status"] == "imported"
    store.close()


def test_a_plan_chain_name_the_store_cannot_hold_is_refused_and_not_logged_by_name(
    tmp_path, caplog
):
    class _Source:
        def list_chains(self, namespace):
            return ["bad:name"]

        def read_chain(self, namespace, chain):  # pragma: no cover - must not run
            raise AssertionError("an invalid chain name must not be read")

    store, target = _target(tmp_path)
    with caplog.at_level(logging.WARNING, logger="pipeline.store_import_audit"):
        report = import_audit_chains(_Source(), target)

    assert report == [
        {
            "chain": "bad:name",
            "records": 0,
            "status": "refused",
            "reason": "invalid_name",
        }
    ]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings
    assert all("bad:name" not in r.getMessage() for r in warnings)
    store.close()


# -- import_chain: the storage's own checks ----------------------------------
def test_import_chain_refuses_a_gap_in_seq_and_writes_nothing(tmp_path):
    store, target = _target(tmp_path)
    records = _chain({"action": "a"}, {"action": "b"}, {"action": "c"})
    gapped = [records[0], records[2]]

    with pytest.raises(ValueError):
        target.import_chain(DEFAULT_NAMESPACE, "gapped", gapped)

    assert target.read_chain(DEFAULT_NAMESPACE, "gapped") == []
    store.close()


def test_import_chain_refuses_a_wrong_prev_hash_and_writes_nothing(tmp_path):
    store, target = _target(tmp_path)
    records = _chain({"action": "a"}, {"action": "b"})
    records[1]["prev_hash"] = "f" * 64
    records[1]["hash"] = audit_chain.record_hash(records[1])

    with pytest.raises(ValueError):
        target.import_chain(DEFAULT_NAMESPACE, "linkage", records)

    assert target.read_chain(DEFAULT_NAMESPACE, "linkage") == []
    store.close()


def test_import_chain_refuses_a_wrong_hash_and_writes_nothing(tmp_path):
    store, target = _target(tmp_path)
    records = _chain({"action": "a"}, {"action": "b"})
    records[1]["hash"] = "0" * 64

    with pytest.raises(ValueError):
        target.import_chain(DEFAULT_NAMESPACE, "forged", records)

    assert target.read_chain(DEFAULT_NAMESPACE, "forged") == []
    store.close()


def test_import_chain_refuses_a_non_empty_chain_and_leaves_its_rows(tmp_path):
    store, target = _target(tmp_path)
    target.append(DEFAULT_NAMESPACE, "busy", {"action": "already.here"})
    before = target.read_chain(DEFAULT_NAMESPACE, "busy")

    with pytest.raises(AuditChainConflictError):
        target.import_chain(DEFAULT_NAMESPACE, "busy", _chain({"action": "x"}))

    assert target.read_chain(DEFAULT_NAMESPACE, "busy") == before
    store.close()


# -- namespaces and structure ------------------------------------------------
def test_a_non_default_namespace_is_refused_before_any_read(tmp_path):
    plans = _plans(tmp_path)
    source = _source(plans)
    store, target = _target(tmp_path)
    reads: list[str] = []
    original = source.read_chain

    def spy(namespace, chain):
        reads.append(chain)
        return original(namespace, chain)

    source.read_chain = spy  # type: ignore[method-assign]

    with pytest.raises(UnknownNamespaceError):
        import_audit_chains(source, target, namespace="other")

    assert reads == []
    store.close()


def test_import_chain_has_exactly_two_callers_in_the_tree():
    callers = set()
    for root in (PACKAGE, APP):
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if IMPORT_CHAIN_CALL.search(path.read_text(encoding="utf-8")):
                callers.add(path.relative_to(PACKAGE.parent).as_posix())

    assert callers == {
        "pipeline/store_sqlite_audit.py",
        "pipeline/store_import_audit.py",
    }


# -- the anchor: a chain must agree with the head anchored for it ------------
def test_a_chain_longer_than_its_anchor_imports(tmp_path, monkeypatch):
    source = _source(_plans(tmp_path))
    store, target = _target(tmp_path)
    _configure_anchor(
        monkeypatch, tmp_path, {LEGACY_PLAN: _anchor_of(source, LEGACY_PLAN, 0)}
    )

    report = import_audit_chains(source, target)

    assert _statuses(report)[LEGACY_PLAN] == "imported"
    assert _row_count(target, LEGACY_PLAN) == 2
    store.close()


def test_a_chain_exactly_at_its_anchor_imports(tmp_path, monkeypatch):
    source = _source(_plans(tmp_path))
    store, target = _target(tmp_path)
    _configure_anchor(
        monkeypatch, tmp_path, {LEGACY_PLAN: _anchor_of(source, LEGACY_PLAN, -1)}
    )

    report = import_audit_chains(source, target)

    assert _statuses(report)[LEGACY_PLAN] == "imported"
    store.close()


def test_a_chain_truncated_below_its_anchored_seq_is_refused_and_nothing_written(
    tmp_path, monkeypatch
):
    plans = _plans(tmp_path)
    _configure_anchor(
        monkeypatch,
        tmp_path,
        {LEGACY_PLAN: _anchor_of(_source(plans), LEGACY_PLAN, -1)},
    )
    stream = plans / f"{LEGACY_PLAN}{SUFFIX}"
    # Cut the newest record: the remaining one-record chain still verifies.
    stream.write_text(stream.read_text().splitlines()[0] + "\n")
    store, target = _target(tmp_path)

    report = {e["chain"]: e for e in import_audit_chains(_source(plans), target)}

    assert report[LEGACY_PLAN]["status"] == "refused"
    assert report[LEGACY_PLAN]["reason"] == "anchor_mismatch"
    assert _row_count(target, LEGACY_PLAN) == 0
    assert report[HOST_CHAIN_ID]["status"] == "imported"
    store.close()


def test_a_truncated_chain_is_refused_on_a_dry_run_too(tmp_path, monkeypatch):
    plans = _plans(tmp_path)
    _configure_anchor(
        monkeypatch,
        tmp_path,
        {LEGACY_PLAN: _anchor_of(_source(plans), LEGACY_PLAN, -1)},
    )
    stream = plans / f"{LEGACY_PLAN}{SUFFIX}"
    stream.write_text(stream.read_text().splitlines()[0] + "\n")
    store, target = _target(tmp_path)

    report = {
        e["chain"]: e for e in import_audit_chains(_source(plans), target, dry_run=True)
    }

    assert report[LEGACY_PLAN]["reason"] == "anchor_mismatch"
    assert _row_count(target, LEGACY_PLAN) == 0
    store.close()


def test_a_chain_rewritten_and_rehashed_under_a_different_hash_is_refused(
    tmp_path, monkeypatch
):
    source = _source(_plans(tmp_path))
    _configure_anchor(
        monkeypatch, tmp_path, {LEGACY_PLAN: _anchor_of(source, LEGACY_PLAN, -1)}
    )
    # Self-consistent and as long as the anchor, but re-linked: seq 1 differs.
    forged = _chain({"action": "forged.one"}, {"action": "forged.two"})
    store, target = _target(tmp_path)

    report = {
        e["chain"]: e
        for e in import_audit_chains(_FixedSource({LEGACY_PLAN: forged}), target)
    }

    assert report[LEGACY_PLAN]["status"] == "refused"
    assert report[LEGACY_PLAN]["reason"] == "anchor_mismatch"
    assert _row_count(target, LEGACY_PLAN) == 0
    store.close()


def test_the_host_chain_is_anchored_under_the_host_key(tmp_path, monkeypatch):
    # The import names the host chain "@host"; the anchor log keys it "host".
    source = _source(_plans(tmp_path))
    _configure_anchor(monkeypatch, tmp_path, {"host": (1, "0" * 64)})
    store, target = _target(tmp_path)

    report = {e["chain"]: e for e in import_audit_chains(source, target)}

    assert report[HOST_CHAIN_ID]["status"] == "refused"
    assert report[HOST_CHAIN_ID]["reason"] == "anchor_mismatch"
    assert _row_count(target, HOST_CHAIN_ID) == 0
    store.close()


def test_a_chain_with_no_anchor_imports_whatever_the_other_anchors_say(
    tmp_path, monkeypatch
):
    source = _source(_plans(tmp_path))
    _configure_anchor(monkeypatch, tmp_path, {"elsewhere": (9, "f" * 64)})
    store, target = _target(tmp_path)

    report = import_audit_chains(source, target)

    assert _statuses(report) == {LEGACY_PLAN: "imported", HOST_CHAIN_ID: "imported"}
    store.close()


def test_no_anchor_sink_configured_imports_as_before(tmp_path, monkeypatch):
    source = _source(_plans(tmp_path))
    monkeypatch.delenv(PIPELINE_AUDIT_ANCHOR, raising=False)
    store, target = _target(tmp_path)

    report = import_audit_chains(source, target)

    assert _statuses(report) == {LEGACY_PLAN: "imported", HOST_CHAIN_ID: "imported"}
    store.close()


def test_a_configured_anchor_log_that_cannot_be_read_refuses_every_chain(
    tmp_path, monkeypatch
):
    source = _source(_plans(tmp_path))
    unreadable = tmp_path / "anchor-as-a-directory"
    unreadable.mkdir()
    monkeypatch.setenv(PIPELINE_AUDIT_ANCHOR, f"file:{unreadable}")
    store, target = _target(tmp_path)

    report = import_audit_chains(source, target)

    assert {e["chain"]: (e["status"], e["reason"]) for e in report} == {
        LEGACY_PLAN: ("refused", "anchor_unreadable"),
        HOST_CHAIN_ID: ("refused", "anchor_unreadable"),
    }
    assert _row_count(target, LEGACY_PLAN) == 0
    assert _row_count(target, HOST_CHAIN_ID) == 0
    store.close()


@pytest.mark.parametrize("value", ["file:relative/anchor.jsonl", "s3://bucket/anchor"])
def test_an_invalid_anchor_sink_fails_closed_and_refuses_every_chain(
    tmp_path, monkeypatch, value
):
    source = _source(_plans(tmp_path))
    monkeypatch.setenv(PIPELINE_AUDIT_ANCHOR, value)
    store, target = _target(tmp_path)

    report = import_audit_chains(source, target)

    assert {e["reason"] for e in report} == {"anchor_unreadable"}
    assert _row_count(target, LEGACY_PLAN) == 0
    store.close()
