"""The AnchorSink protocol and the built-in ``file:`` sink (ADR 0012 decision 10).

The sink is the boundary between the pipeline and wherever anchor heads are
kept - an object-lock bucket, a syslog relay, a SIEM, or the built-in append-only
file. These tests pin the two halves of that boundary: what a sink is allowed to
do (publish a head, hand back what it holds - nothing else), and the exact bytes
the built-in one writes, because an anchor log written by an older pipeline has
to keep verifying against a newer one.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

import pipeline.server  # noqa: F401 - pre-existing import cycle; see test_triage_cap_silent_skip
from pipeline import audit, audit_anchor, paths, persistence
from pipeline.audit_anchor import (
    AnchorHead,
    AnchorSink,
    AnchorUnreadable,
    FileAnchorSink,
    build_anchor_sink,
)
from pipeline.store_records import DEFAULT_NAMESPACE, UnknownNamespaceError

ANCHOR_VARIABLE = audit_anchor.PIPELINE_AUDIT_ANCHOR
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "audit"

#: The instant every byte-compatibility test writes, so the line is fixed.
FROZEN_AT = datetime(2026, 11, 7, 12, 34, 56, 789000, tzinfo=UTC)


class _FrozenDatetime:
    """Stand-in for the module's ``datetime``: the clock does not move."""

    @staticmethod
    def now(tz: object = None) -> datetime:
        assert tz is UTC
        return FROZEN_AT


@pytest.fixture()
def plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the audit streams at a temp plan directory for one test.

    ``pipeline.audit`` reads ``persistence.PLAN_DIR`` and ``pipeline.watchers``
    reads ``paths.PLAN_DIR`` at call time, so patching the module attributes
    redirects them without capturing the operator's real plan directory.
    """
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(persistence, "PLAN_DIR", directory)
    monkeypatch.setattr(paths, "PLAN_DIR", directory)
    monkeypatch.delenv(ANCHOR_VARIABLE, raising=False)
    return directory


def _legacy_line(record: dict[str, object]) -> str:
    """The exact line origin/master's ``_append`` writes for *record*."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str) + "\n"


# --------------------------------------------------------------------------
# The protocol surface
# --------------------------------------------------------------------------


def test_the_protocol_offers_exactly_publish_and_read_heads() -> None:
    """A sink is append-and-read only: no rewrite, no delete, no storage handle."""
    surface = {name for name in vars(AnchorSink) if not name.startswith("_")}
    assert surface == {"publish", "read_heads"}


def test_the_built_in_sink_satisfies_the_protocol(tmp_path: Path) -> None:
    sink = FileAnchorSink(tmp_path / "heads.jsonl")
    assert hasattr(sink, "publish")
    assert list(sink.read_heads()) == []


# --------------------------------------------------------------------------
# Byte compatibility with the anchor log the previous pipeline wrote
# --------------------------------------------------------------------------


def test_publish_writes_the_legacy_line_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One publish is exactly the canonical line ``_append`` used to write."""
    monkeypatch.setattr(audit_anchor, "datetime", _FrozenDatetime)
    path = tmp_path / "heads.jsonl"

    FileAnchorSink(path).publish("default", "p", 3, "h")

    expected = _legacy_line(
        {"chain": "p", "seq": 3, "hash": "h", "at": FROZEN_AT.isoformat()}
    )
    assert path.read_text() == expected
    assert path.read_text() == (
        '{"at":"2026-11-07T12:34:56.789000+00:00","chain":"p","hash":"h","seq":3}\n'
    )


def test_publish_stamps_the_timestamp_the_pipeline_stamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``at`` field is the caller's UTC ISO timestamp, not a sink invention."""
    monkeypatch.setattr(audit_anchor, "datetime", _FrozenDatetime)
    path = tmp_path / "heads.jsonl"

    FileAnchorSink(path).publish("default", "p", 0, "abc")

    assert json.loads(path.read_text())["at"] == FROZEN_AT.isoformat()


def test_the_timestamp_is_the_one_a_pipeline_pass_writes(
    plan_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass and its sink stamp the same format, so a mixed log keeps verifying."""
    monkeypatch.setattr(audit_anchor, "datetime", _FrozenDatetime)
    audit.append_audit("p", action="dispatch", outcome="ok")
    path = tmp_path / "anchor" / "heads.jsonl"
    monkeypatch.setenv(ANCHOR_VARIABLE, f"file:{path}")

    audit_anchor.anchor_heads()

    assert json.loads(path.read_text())["at"] == FROZEN_AT.isoformat()


def test_a_legacy_anchor_log_reads_back_the_same_heads() -> None:
    """A log written by origin/master is read as the heads it recorded."""
    sink = FileAnchorSink(FIXTURES / "legacy.anchor.jsonl")

    assert list(sink.read_heads()) == [
        AnchorHead(DEFAULT_NAMESPACE, "p", 0, _legacy_hash("legacy-anchor-p-0")),
        AnchorHead(DEFAULT_NAMESPACE, "p", 1, _legacy_hash("legacy-anchor-p-1")),
        AnchorHead(DEFAULT_NAMESPACE, "host", 0, _legacy_hash("legacy-anchor-host-0")),
        AnchorHead(DEFAULT_NAMESPACE, "decisions:p", 0, _legacy_hash("legacy-anchor-d-0")),
    ]


def _legacy_hash(seed: str) -> str:
    """The 64-hex stand-in hashes the committed fixture carries."""
    import hashlib

    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def test_a_missing_log_reads_as_no_heads(tmp_path: Path) -> None:
    sink = FileAnchorSink(tmp_path / "absent" / "heads.jsonl")

    assert list(sink.read_heads()) == []


def test_heads_are_yielded_in_publication_order(tmp_path: Path) -> None:
    path = tmp_path / "heads.jsonl"
    sink = FileAnchorSink(path)
    sink.publish(DEFAULT_NAMESPACE, "beta", 4, "bb")
    sink.publish(DEFAULT_NAMESPACE, "alpha", 7, "aa")

    assert [(head.chain, head.seq) for head in sink.read_heads()] == [
        ("beta", 4),
        ("alpha", 7),
    ]


# --------------------------------------------------------------------------
# build_anchor_sink: off by default, one accepted form
# --------------------------------------------------------------------------


def test_unset_and_empty_build_no_sink() -> None:
    assert build_anchor_sink({}) is None
    assert build_anchor_sink({ANCHOR_VARIABLE: ""}) is None
    assert build_anchor_sink({ANCHOR_VARIABLE: "   "}) is None


def test_an_absolute_file_path_builds_the_file_sink(tmp_path: Path) -> None:
    path = tmp_path / "anchor" / "heads.jsonl"

    sink = build_anchor_sink({ANCHOR_VARIABLE: f"file:{path}"})

    assert isinstance(sink, FileAnchorSink)
    assert sink.path == path


def test_an_unknown_scheme_is_refused_with_the_existing_message() -> None:
    with pytest.raises(ValueError) as refused:
        build_anchor_sink({ANCHOR_VARIABLE: "s3:bucket"})

    assert str(refused.value) == (
        f"{ANCHOR_VARIABLE} must be 'file:<absolute path>'; "
        "audit-head anchoring is off until it is corrected"
    )
    assert "s3:bucket" not in str(refused.value)


def test_a_relative_path_is_refused_with_the_existing_message() -> None:
    with pytest.raises(ValueError) as refused:
        build_anchor_sink({ANCHOR_VARIABLE: "file:relative/x"})

    assert str(refused.value) == (
        f"{ANCHOR_VARIABLE} 'file:' path must be absolute; "
        "audit-head anchoring is off until it is corrected"
    )
    assert "relative/x" not in str(refused.value)


def test_a_path_inside_the_plan_directory_is_refused(plan_dir: Path) -> None:
    """An anchor the pipeline can rewrite proves nothing."""
    value = f"file:{plan_dir / 'heads.jsonl'}"

    with pytest.raises(ValueError) as refused:
        build_anchor_sink({ANCHOR_VARIABLE: value})

    assert str(refused.value) == (
        f"{ANCHOR_VARIABLE} 'file:' path must be outside the plan directory; "
        "an anchor the pipeline can rewrite proves nothing"
    )
    assert str(plan_dir) not in str(refused.value)
    assert value not in str(refused.value)


def test_the_environment_is_read_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ANCHOR_VARIABLE, raising=False)
    assert build_anchor_sink() is None
    path = tmp_path / "anchor" / "heads.jsonl"
    monkeypatch.setenv(ANCHOR_VARIABLE, f"file:{path}")

    sink = build_anchor_sink()

    assert isinstance(sink, FileAnchorSink)
    assert sink.path == path


# --------------------------------------------------------------------------
# The sink file is part of the tamper-evidence boundary
# --------------------------------------------------------------------------


def test_publish_refuses_a_symlinked_sink_and_writes_nothing(tmp_path: Path) -> None:
    """A symlink planted in a writable sink directory is not followed."""
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("untouched\n")
    link = tmp_path / "heads.jsonl"
    link.symlink_to(target)

    with pytest.raises(OSError):
        FileAnchorSink(link).publish(DEFAULT_NAMESPACE, "p", 0, "h")

    assert target.read_text() == "untouched\n"


def test_publish_tightens_a_loose_mode(tmp_path: Path) -> None:
    """A pre-existing 0o644 log is re-imposed to 0o600 on the open descriptor."""
    path = tmp_path / "heads.jsonl"
    path.write_text("")
    path.chmod(0o644)

    FileAnchorSink(path).publish(DEFAULT_NAMESPACE, "p", 0, "h")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_publish_creates_the_sink_six_hundred(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "heads.jsonl"

    FileAnchorSink(path).publish(DEFAULT_NAMESPACE, "p", 0, "h")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_publish_only_appends(tmp_path: Path) -> None:
    path = tmp_path / "heads.jsonl"
    sink = FileAnchorSink(path)
    sink.publish(DEFAULT_NAMESPACE, "p", 0, "a")
    before = path.read_bytes()

    sink.publish(DEFAULT_NAMESPACE, "p", 1, "b")

    assert path.read_bytes().startswith(before)
    assert len(path.read_text().splitlines()) == 2


# --------------------------------------------------------------------------
# The sink stores the default namespace only
# --------------------------------------------------------------------------


def test_publish_refuses_another_namespace_and_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "heads.jsonl"
    sink = FileAnchorSink(path)

    with pytest.raises(UnknownNamespaceError):
        sink.publish("acme", "p", 0, "h")

    assert not path.exists()


def test_publish_refuses_another_namespace_on_an_existing_log(tmp_path: Path) -> None:
    path = tmp_path / "heads.jsonl"
    sink = FileAnchorSink(path)
    sink.publish(DEFAULT_NAMESPACE, "p", 0, "h")
    before = path.read_bytes()

    with pytest.raises(UnknownNamespaceError):
        sink.publish("acme", "p", 1, "i")

    assert path.read_bytes() == before


def test_read_heads_reports_the_default_namespace(tmp_path: Path) -> None:
    path = tmp_path / "heads.jsonl"
    FileAnchorSink(path).publish(DEFAULT_NAMESPACE, "p", 0, "h")

    assert [head.namespace for head in FileAnchorSink(path).read_heads()] == [
        DEFAULT_NAMESPACE
    ]


# --------------------------------------------------------------------------
# An unreadable log is not an empty one
# --------------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits")
def test_an_unreadable_log_raises_rather_than_reading_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "heads.jsonl"
    FileAnchorSink(path).publish(DEFAULT_NAMESPACE, "p", 0, "h")
    path.chmod(0o000)

    with pytest.raises(AnchorUnreadable) as refused:
        list(FileAnchorSink(path).read_heads())

    assert refused.value.error_type == "PermissionError"
    assert str(path) not in str(refused.value)


def test_a_directory_in_place_of_the_log_raises(tmp_path: Path) -> None:
    path = tmp_path / "heads.jsonl"
    path.mkdir()

    with pytest.raises(AnchorUnreadable):
        list(FileAnchorSink(path).read_heads())


def test_a_read_that_fails_mid_log_is_not_a_partial_head_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure part-way through must not be mistaken for the whole log."""
    path = tmp_path / "heads.jsonl"
    sink = FileAnchorSink(path)
    sink.publish(DEFAULT_NAMESPACE, "p", 0, "a")
    sink.publish(DEFAULT_NAMESPACE, "p", 1, "b")
    real_parsed = audit_anchor._parsed

    def _breaks_after_first(handle):
        for index, record in enumerate(real_parsed(handle)):
            if index == 1:
                raise OSError(5, "input/output error")
            yield record

    monkeypatch.setattr(audit_anchor, "_parsed", _breaks_after_first)

    with pytest.raises(AnchorUnreadable):
        list(FileAnchorSink(path).read_heads())


def test_malformed_lines_are_not_heads(tmp_path: Path) -> None:
    path = tmp_path / "heads.jsonl"
    path.write_text(
        json.dumps({"chain": "p", "seq": None, "hash": ""}) + "\n"
        + json.dumps({"chain": "p", "seq": "7", "hash": "abc"}) + "\n"
        + "not json\n"
        + json.dumps({"chain": "", "seq": 0, "hash": "abc"}) + "\n"
    )

    assert list(FileAnchorSink(path).read_heads()) == []


# --------------------------------------------------------------------------
# The documented contract
# --------------------------------------------------------------------------


def test_reference_documents_the_sink_as_the_built_in_one() -> None:
    root = Path(__file__).resolve().parent.parent.parent
    text = (root / "REFERENCE.md").read_text()

    # The accepted form is still exactly one, and it is still the file sink.
    assert "`file:<absolute path>` appends each chain's head" in text
    assert "Any other value" in text
    # ... and the sink is now a protocol with the file sink as the built-in one.
    assert "The sink is an `AnchorSink`" in text
    assert "`file:` is the built-in one" in text


def test_the_module_still_says_file_is_the_only_accepted_form() -> None:
    doc = audit_anchor.__doc__ or ""

    assert "``file:<absolute path>`` is the only accepted form" in doc
