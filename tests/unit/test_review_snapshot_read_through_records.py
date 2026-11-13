"""Review reads recorded context snapshots through the record store.

``_read_recorded_snapshot`` used to open ``PLAN_DIR/context/<hex>/content.txt``
itself. It now asks ``_store.records().get_context_snapshot("default", digest)``
and keeps today's answers: a missing, unverifiable, or non-digest snapshot is None.
"""

from __future__ import annotations

import hashlib

import pytest

import pipeline.review as review_mod
from pipeline import server

TEXT = "Always use the shared Button component."
DIGEST = "sha256:" + hashlib.sha256(TEXT.encode("utf-8")).hexdigest()


class _FakeRecords:
    def __init__(self):
        self.snapshots = {}
        self.calls = []

    def get_context_snapshot(self, namespace, digest):
        self.calls.append((namespace, digest))
        return self.snapshots.get(digest)


class _FakeStore:
    def __init__(self, records):
        self._records = records

    def records(self):
        return self._records


@pytest.fixture
def records(monkeypatch):
    fake = _FakeRecords()
    monkeypatch.setattr(server, "_store", _FakeStore(fake))
    return fake


def test_should_serve_recorded_snapshot_to_the_reviewer(records):
    records.snapshots[DIGEST] = {"content": TEXT, "meta": {}}

    assert review_mod._read_recorded_snapshot({"digest": DIGEST}) == TEXT
    assert records.calls == [("default", DIGEST)]


def test_should_return_none_when_snapshot_is_missing(records):
    assert review_mod._read_recorded_snapshot({"digest": DIGEST}) is None
    assert records.calls == [("default", DIGEST)]


def test_should_return_none_when_recorded_content_no_longer_matches_digest(records):
    records.snapshots[DIGEST] = {"content": "tampered", "meta": {}}

    assert review_mod._read_recorded_snapshot({"digest": DIGEST}) is None


@pytest.mark.parametrize(
    "digest",
    [
        "",
        "sha256:",
        "sha256:../../etc/passwd",
        "sha256:" + "AB" * 32,
        "sha256:" + "z" * 64,
        "sha256:" + "a" * 63,
        DIGEST.removeprefix("sha256:"),
    ],
)
def test_should_refuse_non_digest_before_reaching_storage(records, digest):
    assert review_mod._read_recorded_snapshot({"digest": digest}) is None
    assert records.calls == []
