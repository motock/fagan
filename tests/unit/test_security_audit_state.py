"""Per-repo record of the last security audit (the state behind the due notice).

The state is a record of the namespaced store, so these tests drive it through
a records object. The file layout is pinned by the FileRecords contract, not
here.
"""

import re
from datetime import UTC, datetime

import pytest

from pipeline import security_audit
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE

_SHA = "a" * 40
_NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@pytest.fixture
def records():
    return InMemoryRecordStore()


def test_repo_key_is_sixteen_lowercase_hex_characters():
    assert re.fullmatch(r"[0-9a-f]{16}", security_audit.repo_key("/repos/app"))


def test_repo_key_is_stable_for_the_same_repo():
    assert security_audit.repo_key("/repos/app") == security_audit.repo_key("/repos/app")


def test_repo_key_ignores_a_trailing_slash():
    assert security_audit.repo_key("/repos/app/") == security_audit.repo_key("/repos/app")


def test_repo_key_differs_between_repos():
    assert security_audit.repo_key("/repos/a") != security_audit.repo_key("/repos/b")


def test_read_returns_none_when_no_audit_was_recorded(records):
    assert security_audit.read_audit_state(records, "/repos/app") is None


def test_record_then_read_round_trips_the_state(records):
    recorded = security_audit.record_audit(records, "/repos/app", _SHA, _NOW)

    assert recorded == {
        "repo_root": "/repos/app",
        "last_audited_sha": _SHA,
        "last_audited_at": _NOW.isoformat(),
    }
    assert security_audit.read_audit_state(records, "/repos/app") == recorded


def test_record_is_kept_under_the_default_namespace_and_the_repo_key(records):
    security_audit.record_audit(records, "/repos/app", _SHA, _NOW)

    assert records.get_security_audit_state(
        DEFAULT_NAMESPACE, security_audit.repo_key("/repos/app")
    ) == security_audit.read_audit_state(records, "/repos/app")


def test_a_second_record_replaces_the_first(records):
    security_audit.record_audit(records, "/repos/app", _SHA, _NOW)

    security_audit.record_audit(records, "/repos/app", "b" * 40, _NOW)

    assert security_audit.read_audit_state(records, "/repos/app")["last_audited_sha"] == "b" * 40


@pytest.mark.parametrize("sha", ["", "not-a-sha", "abc123", "g" * 40, "A" * 40, "a" * 65])
def test_record_rejects_a_malformed_sha_and_records_nothing(records, sha):
    with pytest.raises(ValueError, match="sha"):
        security_audit.record_audit(records, "/repos/app", sha, _NOW)

    assert security_audit.read_audit_state(records, "/repos/app") is None


def test_record_accepts_the_shortest_and_longest_valid_sha(records):
    security_audit.record_audit(records, "/repos/a", "a" * 7, _NOW)
    security_audit.record_audit(records, "/repos/b", "a" * 64, _NOW)

    assert security_audit.read_audit_state(records, "/repos/a")["last_audited_sha"] == "a" * 7
    assert security_audit.read_audit_state(records, "/repos/b")["last_audited_sha"] == "a" * 64


def test_record_rejects_an_empty_repo_root_and_records_nothing(records):
    with pytest.raises(ValueError, match="repo_root"):
        security_audit.record_audit(records, "", _SHA, _NOW)

    assert records.get_security_audit_state(DEFAULT_NAMESPACE, security_audit.repo_key("")) is None


def test_record_rejects_a_naive_timestamp_and_records_nothing(records):
    with pytest.raises(ValueError, match="timezone"):
        security_audit.record_audit(records, "/repos/app", _SHA, _NOW.replace(tzinfo=None))

    assert security_audit.read_audit_state(records, "/repos/app") is None
