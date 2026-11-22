"""Unit tests for pipeline.checkpoint_drop: the agent-side write and the
control-plane claim of a checkpoint dropped as a file in the worktree.

The drop is written by untrusted code, so every refusal path is asserted:
the refused drop is deleted, its content never reaches the log, and a
symlink or directory of the same name is never followed.
"""

import json
import logging
import os
import stat
from pathlib import Path

import pytest

from pipeline import checkpoint_drop

DROP = ".agent_checkpoint"
CLAIMED = ".agent_checkpoint.claimed"
HEX40 = "a" * 40
HEX64 = "0123456789abcdef" * 4
MARKER = "SECRET-MARKER-7f3a"


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    directory = tmp_path / "wt"
    directory.mkdir()
    return directory


def _put(worktree: Path, payload) -> None:
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    (worktree / DROP).write_bytes(data)


def _valid(**overrides) -> dict:
    record = {"step": "build", "summary": "did it", "next_hint": "next", "commit": None}
    record.update(overrides)
    return record


def _reason_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == checkpoint_drop.__name__]


# ---------- write / claim round trip ----------


def test_write_then_claim_round_trips_the_fields(worktree: Path):
    checkpoint_drop.write(worktree, "step-one", "summary text", "hint text", commit=HEX40)

    record = checkpoint_drop.claim(worktree)

    assert record == {
        "step": "step-one",
        "summary": "summary text",
        "next_hint": "hint text",
        "commit": HEX40,
    }


def test_claim_removes_the_drop_and_the_claimed_file(worktree: Path):
    checkpoint_drop.write(worktree, "step-one", "summary")

    checkpoint_drop.claim(worktree)

    assert not (worktree / DROP).exists()
    assert not (worktree / CLAIMED).exists()


def test_write_leaves_only_the_drop_with_owner_only_mode(worktree: Path):
    checkpoint_drop.write(worktree, "step-one", "summary")

    assert sorted(p.name for p in worktree.iterdir()) == [DROP]
    assert stat.S_IMODE((worktree / DROP).stat().st_mode) == 0o600


def test_claim_with_no_drop_returns_none(worktree: Path):
    assert checkpoint_drop.claim(worktree) is None


# ---------- refused and deleted ----------


@pytest.mark.parametrize(
    ("label", "payload", "reason"),
    [
        ("too_large", b'{"step": "' + MARKER.encode() + b'", "pad": "' + b"x" * 16400 + b'"}', "too_large"),
        ("not_json", ("not json " + MARKER).encode(), "not_json"),
        ("json_list", json.dumps([MARKER]).encode(), "bad_field"),
        ("missing_step", {"summary": MARKER}, "bad_field"),
        ("empty_step", _valid(step="   "), "bad_field"),
        ("long_step", _valid(step="s" * 121), "bad_field"),
        ("int_summary", _valid(summary=7), "bad_field"),
    ],
)
def test_refused_drop_is_deleted_and_logs_only_the_reason_code(
    worktree: Path, caplog, label, payload, reason
):
    _put(worktree, payload)
    caplog.set_level(logging.WARNING, logger=checkpoint_drop.__name__)

    assert checkpoint_drop.claim(worktree) is None

    assert not (worktree / DROP).exists()
    assert not (worktree / CLAIMED).exists()
    records = _reason_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].reason == reason
    assert MARKER not in caplog.text
    assert "x" * 100 not in caplog.text


def test_drop_at_the_size_limit_is_accepted(worktree: Path):
    # An unknown key pads the object to exactly the 16384-byte limit.
    base = json.dumps(_valid(pad=""))
    payload = json.dumps(_valid(pad="x" * (16384 - len(base)))).encode()
    assert len(payload) == 16384
    _put(worktree, payload)

    assert checkpoint_drop.claim(worktree) is not None


# ---------- refused without following ----------


def test_symlink_drop_is_refused_and_its_target_is_not_read(
    worktree: Path, tmp_path: Path, caplog
):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_valid(summary=MARKER)))
    (worktree / DROP).symlink_to(outside)
    caplog.set_level(logging.WARNING, logger=checkpoint_drop.__name__)

    assert checkpoint_drop.claim(worktree) is None

    assert outside.exists()
    assert outside.read_text() == json.dumps(_valid(summary=MARKER))
    assert not os.path.lexists(worktree / DROP)
    assert _reason_records(caplog)[0].reason == "not_regular"
    assert MARKER not in caplog.text


def test_directory_drop_is_refused_and_left_alone_as_a_name(worktree: Path, caplog):
    (worktree / DROP).mkdir()
    (worktree / DROP / "inner.txt").write_text(MARKER)
    caplog.set_level(logging.WARNING, logger=checkpoint_drop.__name__)

    assert checkpoint_drop.claim(worktree) is None

    assert not os.path.lexists(worktree / DROP)
    assert not os.path.lexists(worktree / (DROP + ".claimed"))
    assert _reason_records(caplog)[0].reason == "not_regular"
    assert MARKER not in caplog.text


def test_non_empty_directory_drop_does_not_block_the_next_valid_drop(worktree: Path):
    (worktree / DROP).mkdir()
    (worktree / DROP / "inner.txt").write_text(MARKER)
    assert checkpoint_drop.claim(worktree) is None

    checkpoint_drop.write(worktree, step="after", summary="s")

    record = checkpoint_drop.claim(worktree)
    assert record is not None
    assert record["step"] == "after"


def test_stale_non_empty_claimed_directory_does_not_block_a_valid_drop(worktree: Path):
    (worktree / (DROP + ".claimed")).mkdir()
    (worktree / (DROP + ".claimed") / "inner.txt").write_text(MARKER)
    _put(worktree, _valid(step="fresh"))

    record = checkpoint_drop.claim(worktree)

    assert record is not None
    assert record["step"] == "fresh"
    assert not os.path.lexists(worktree / DROP)
    assert not os.path.lexists(worktree / (DROP + ".claimed"))


def test_symlink_at_claimed_name_is_removed_and_its_target_is_untouched(
    worktree: Path, tmp_path: Path
):
    outside = tmp_path / "outside.txt"
    outside.write_text(MARKER)
    (worktree / (DROP + ".claimed")).symlink_to(outside)
    _put(worktree, _valid(step="fresh"))

    record = checkpoint_drop.claim(worktree)

    assert record is not None
    assert outside.read_text() == MARKER
    assert not os.path.lexists(worktree / (DROP + ".claimed"))


# ---------- commit field ----------


@pytest.mark.parametrize(
    "commit",
    ["HEAD", "a" * 39, "A" * 40, 123, "g" * 40],
)
def test_invalid_commit_becomes_none_and_the_checkpoint_is_still_accepted(worktree: Path, commit):
    _put(worktree, _valid(commit=commit))

    record = checkpoint_drop.claim(worktree)

    assert record is not None
    assert record["step"] == "build"
    assert record["commit"] is None


def test_sha256_length_commit_is_kept(worktree: Path):
    _put(worktree, _valid(commit=HEX64))

    assert checkpoint_drop.claim(worktree)["commit"] == HEX64


def test_unknown_keys_are_ignored(worktree: Path):
    _put(worktree, _valid(extra_field=MARKER))

    record = checkpoint_drop.claim(worktree)

    assert record is not None
    assert "extra_field" not in record


# ---------- claim race ----------


def test_two_claims_of_one_drop_return_the_record_once(worktree: Path):
    _put(worktree, _valid())

    first = checkpoint_drop.claim(worktree)
    second = checkpoint_drop.claim(worktree)

    assert first is not None
    assert second is None


# ---------- file mode switch ----------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        ("", False),
        ("tool", False),
        ("FILE ", True),
        ("file", True),
        ("files", False),
    ],
)
def test_is_file_mode_reads_the_stripped_lowercased_variable(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(checkpoint_drop.MODE_ENV, raising=False)
    else:
        monkeypatch.setenv(checkpoint_drop.MODE_ENV, value)

    assert checkpoint_drop.is_file_mode() is expected


def test_drop_name_and_mode_variable_are_the_documented_names():
    assert checkpoint_drop.DROP_NAME == DROP
    assert checkpoint_drop.MODE_ENV == "PIPELINE_CHECKPOINT_MODE"
