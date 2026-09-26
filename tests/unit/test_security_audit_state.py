"""Per-repo record of the last security audit (the state behind the due notice)."""

import os
from datetime import datetime, timezone

import pytest

from pipeline import security_audit

_SHA = "a" * 40
_NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def test_state_path_is_inside_the_state_dir_and_named_for_security_audit(tmp_path):
    path = security_audit.state_path(tmp_path, "/repos/app")

    assert path.parent == tmp_path
    assert path.name.startswith("security-audit.")
    assert path.name.endswith(".json")


def test_state_path_is_stable_for_the_same_repo(tmp_path):
    first = security_audit.state_path(tmp_path, "/repos/app")

    assert security_audit.state_path(tmp_path, "/repos/app") == first


def test_state_path_ignores_a_trailing_slash(tmp_path):
    assert security_audit.state_path(tmp_path, "/repos/app/") == security_audit.state_path(
        tmp_path, "/repos/app"
    )


def test_state_path_differs_between_repos(tmp_path):
    assert security_audit.state_path(tmp_path, "/repos/a") != security_audit.state_path(
        tmp_path, "/repos/b"
    )


def test_read_returns_none_when_no_audit_was_recorded(tmp_path):
    assert security_audit.read_audit_state(tmp_path, "/repos/app") is None


def test_record_then_read_round_trips_the_state(tmp_path):
    recorded = security_audit.record_audit(tmp_path, "/repos/app", _SHA, _NOW)

    assert recorded == {
        "repo_root": "/repos/app",
        "last_audited_sha": _SHA,
        "last_audited_at": _NOW.isoformat(),
    }
    assert security_audit.read_audit_state(tmp_path, "/repos/app") == recorded


def test_a_second_record_replaces_the_first(tmp_path):
    security_audit.record_audit(tmp_path, "/repos/app", _SHA, _NOW)

    security_audit.record_audit(tmp_path, "/repos/app", "b" * 40, _NOW)

    assert security_audit.read_audit_state(tmp_path, "/repos/app")["last_audited_sha"] == "b" * 40


def test_record_leaves_only_the_state_file_behind(tmp_path):
    security_audit.record_audit(tmp_path, "/repos/app", _SHA, _NOW)

    assert os.listdir(tmp_path) == [security_audit.state_path(tmp_path, "/repos/app").name]


@pytest.mark.parametrize("sha", ["", "not-a-sha", "abc123", "g" * 40, "A" * 40, "a" * 65])
def test_record_rejects_a_malformed_sha(tmp_path, sha):
    with pytest.raises(ValueError, match="sha"):
        security_audit.record_audit(tmp_path, "/repos/app", sha, _NOW)


def test_record_accepts_the_shortest_and_longest_valid_sha(tmp_path):
    security_audit.record_audit(tmp_path, "/repos/a", "a" * 7, _NOW)
    security_audit.record_audit(tmp_path, "/repos/b", "a" * 64, _NOW)

    assert security_audit.read_audit_state(tmp_path, "/repos/a")["last_audited_sha"] == "a" * 7
    assert security_audit.read_audit_state(tmp_path, "/repos/b")["last_audited_sha"] == "a" * 64


def test_record_rejects_an_empty_repo_root(tmp_path):
    with pytest.raises(ValueError, match="repo_root"):
        security_audit.record_audit(tmp_path, "", _SHA, _NOW)


def test_record_rejects_a_naive_timestamp(tmp_path):
    with pytest.raises(ValueError, match="timezone"):
        security_audit.record_audit(tmp_path, "/repos/app", _SHA, _NOW.replace(tzinfo=None))


def test_a_corrupt_state_file_reads_as_no_audit(tmp_path):
    security_audit.state_path(tmp_path, "/repos/app").write_text("{not json")

    assert security_audit.read_audit_state(tmp_path, "/repos/app") is None


@pytest.mark.parametrize("payload", ["[]", '{"repo_root": "/repos/app"}', '{"last_audited_sha": 5}'])
def test_a_state_file_of_the_wrong_shape_reads_as_no_audit(tmp_path, payload):
    security_audit.state_path(tmp_path, "/repos/app").write_text(payload)

    assert security_audit.read_audit_state(tmp_path, "/repos/app") is None
