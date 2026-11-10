"""The story timeline read through the audit log, not through a directory.

``pipeline.service_levels`` used to glob ``*.audit.jsonl`` under a plan
directory and parse the lines itself.  It now reads the chains through an
:class:`pipeline.audit_log.AuditLog`, so the story marks come from the same
surface the writers append to and the module stops knowing the audit file
layout.

What is pinned here:

* the same records read through the in-memory log and through the file log
  produce the same timeline -- the reader does not care which log it is given;
* an empty log yields no timelines at all;
* a broken line (a ``None`` entry, as the file log reports one it cannot
  parse) is skipped, not fatal;
* the host chain is not a plan: a record in it never becomes a story mark.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pipeline import persistence, service_levels
from pipeline.audit_file import FileAuditLog
from pipeline.audit_log import HOST_CHAIN_ID
from pipeline.audit_memory import InMemoryAuditLog
from pipeline.store_records import DEFAULT_NAMESPACE

DISPATCHED = "2025-01-01T00:00:00Z"
REVIEW_STARTED = "2025-01-01T01:00:00Z"
MERGED = "2025-01-01T03:00:00Z"


@pytest.fixture()
def plan_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Redirect the audit streams at a temp plan directory for one test."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    return tmp_path


def _body(action: str, ts: str, story: str = "S1") -> dict:
    """One audit record body, as a writer would append it."""
    return {
        "action": action,
        "outcome": "allowed",
        "ts": ts,
        "target": {"plan": "p", "story": story},
        "detail": {},
    }


def _trio(log, chain: str = "p") -> None:
    """Append the dispatch/review_start/merge trio to one chain."""
    log.append(DEFAULT_NAMESPACE, chain, _body("story.dispatch", DISPATCHED))
    log.append(
        DEFAULT_NAMESPACE,
        chain,
        {
            **_body("story.review_start", REVIEW_STARTED),
            "detail": {"started_at": REVIEW_STARTED},
        },
    )
    log.append(DEFAULT_NAMESPACE, chain, _body("story.merge", MERGED))


EXPECTED = {
    ("p", "S1"): {
        "dispatched": datetime(2025, 1, 1, 0, 0, tzinfo=UTC),
        "review_started": datetime(2025, 1, 1, 1, 0, tzinfo=UTC),
        "merged": datetime(2025, 1, 1, 3, 0, tzinfo=UTC),
    }
}


def test_the_in_memory_and_the_file_log_give_the_same_timeline(plan_dir) -> None:
    """The reader does not care which log it is handed."""
    memory = InMemoryAuditLog()
    _trio(memory)

    on_disk = FileAuditLog()
    _trio(on_disk)

    assert service_levels.read_story_timeline(memory) == EXPECTED
    assert service_levels.read_story_timeline(on_disk) == EXPECTED
    assert service_levels.read_story_timeline(memory) == service_levels.read_story_timeline(
        on_disk
    )


def test_an_empty_log_yields_no_timelines() -> None:
    assert service_levels.read_story_timeline(InMemoryAuditLog()) == {}


def test_a_broken_entry_is_skipped(plan_dir) -> None:
    """A line the log cannot parse is skipped, not fatal.

    The file log reports such a line as ``None`` in the chain; the reader must
    step over it and keep the records around it.
    """
    log = FileAuditLog()
    log.append(DEFAULT_NAMESPACE, "p", _body("story.dispatch", DISPATCHED))
    with (plan_dir / "p.audit.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("{not json\n")
    log.append(DEFAULT_NAMESPACE, "p", _body("story.merge", MERGED))

    timeline = service_levels.read_story_timeline(log)

    assert timeline[("p", "S1")]["dispatched"] == datetime(2025, 1, 1, tzinfo=UTC)
    assert timeline[("p", "S1")]["merged"] == datetime(2025, 1, 1, 3, 0, tzinfo=UTC)


def test_a_host_chain_record_is_not_counted(plan_dir) -> None:
    """The host chain belongs to no plan, so it is not a story."""
    log = InMemoryAuditLog()
    log.append(
        DEFAULT_NAMESPACE,
        HOST_CHAIN_ID,
        _body("story.dispatch", DISPATCHED, story="HOST-SENTINEL"),
    )

    assert service_levels.read_story_timeline(log) == {}


def test_a_plan_record_is_counted_and_the_host_one_is_not(plan_dir) -> None:
    log = InMemoryAuditLog()
    _trio(log)
    log.append(
        DEFAULT_NAMESPACE,
        HOST_CHAIN_ID,
        _body("story.dispatch", DISPATCHED, story="HOST-SENTINEL"),
    )

    assert sorted(service_levels.read_story_timeline(log)) == [("p", "S1")]
