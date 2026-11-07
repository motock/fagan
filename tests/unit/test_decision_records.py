"""Tests for the per-plan decision-record chain (decision record v2).

``pipeline.decision_records`` keeps one hash-chained stream per plan,
``<PLAN_DIR>/<plan>.decisions.chain.jsonl``: an autonomous decision (a merge, an
override, a scope widening, a triage ruling) is recorded with its evidence and
its alternatives, and the chain is anchored by ``pipeline.audit_anchor`` so a
truncated tail is visible from outside the pipeline host.

These tests pin the append/read round trip, the chain links, the read-only and
side-effect-free behaviour of verification and reading, the refusal of
caller-supplied identity fields, the data-minimisation limits (which must name
the field and never the value), the namespace filter, concurrent appends, and
the anchor integration.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from app.identity import Principal
from pipeline import actor, audit, audit_anchor, decision_records, paths, persistence

ANCHOR_VARIABLE = "PIPELINE_AUDIT_ANCHOR"


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture()
def plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``PLAN_DIR`` at a temp directory for one test.

    Both bindings are patched because ``pipeline.audit`` reads
    ``persistence.PLAN_DIR`` while other modules read ``paths.PLAN_DIR``; the
    stream path has to follow the module attribute at call time, never a value
    captured at import, or a test would append to the operator's real plans.
    """
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(persistence, "PLAN_DIR", directory)
    monkeypatch.setattr(paths, "PLAN_DIR", directory)
    monkeypatch.delenv(ANCHOR_VARIABLE, raising=False)
    return directory


@pytest.fixture()
def anchor_file(tmp_path: Path, plan_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Enable anchoring to an append-only log outside ``PLAN_DIR``."""
    directory = tmp_path / "anchor"
    directory.mkdir()
    path = directory / "heads.jsonl"
    monkeypatch.setenv(ANCHOR_VARIABLE, f"file:{path}")
    return path


def _sha(seed: str = "a") -> str:
    return (seed * 64)[:64]


def _record(**overrides: Any) -> dict[str, Any]:
    """A valid caller record; *overrides* replace individual top-level fields."""
    record: dict[str, Any] = {
        "decision_id": "0f" * 16,
        "class": "high_risk_merge",
        "subject": {
            "plan": "p",
            "story": "s1",
            "pr": "42",
            "sha": _sha("b"),
        },
        "evidence": [
            {"kind": "test_run", "ref": "pytest -q", "sha256": _sha("c")},
            {"kind": "note", "ref": "retro/notes.md", "sha256": ""},
        ],
        "rule": {
            "layer_shas": [_sha("d"), _sha("e")],
            "persona_sha": _sha("f"),
            "policy_sha": _sha("1"),
            "model": "claude-sonnet-4",
        },
        "alternatives": ["ask a human", "wait for CI"],
        "ruling": "merge: CI green and the diff is inside the approved scope",
        "tier": "auto+notify",
        "reversible": True,
        "outcome": "",
    }
    record.update(overrides)
    return record


def _chain_path(plan_dir: Path, plan_name: str = "p") -> Path:
    return plan_dir / f"{plan_name}.decisions.chain.jsonl"


def _lines(path: Path) -> list[str]:
    return [line for line in path.read_text().splitlines() if line.strip()]


def _append(plan_name: str = "p", **overrides: Any) -> dict[str, Any]:
    return decision_records.append_decision_record(plan_name, _record(**overrides))


# --------------------------------------------------------------------------
# Append / read round trip
# --------------------------------------------------------------------------


def test_append_then_read_round_trip(plan_dir: Path) -> None:
    stored = _append()

    records = decision_records.read_decision_records("p")

    assert len(records) == 1
    assert records[0] == stored
    assert stored["schema_version"] == 2
    assert stored["namespace"] == "default"
    assert stored["decision_id"] == "0f" * 16
    assert stored["class"] == "high_risk_merge"
    assert stored["ruling"] == _record()["ruling"]
    assert stored["evidence"][1]["sha256"] == ""
    assert stored["hash"] == audit._record_hash(stored)
    assert _chain_path(plan_dir).exists()


def test_read_of_missing_stream_is_empty(plan_dir: Path) -> None:
    assert decision_records.read_decision_records("absent") == []


def test_stored_record_carries_the_server_side_identity_fields(plan_dir: Path) -> None:
    stored = _append()

    for field in (
        "actor",
        "principal_type",
        "client_id",
        "correlation_id",
        "ts",
        "prev_hash",
        "hash",
        "seq",
    ):
        assert field in stored
    assert stored["prev_hash"] == ""


# --------------------------------------------------------------------------
# Chain links
# --------------------------------------------------------------------------


def test_three_appends_link_by_prev_hash_and_seq(plan_dir: Path) -> None:
    first = _append()
    second = _append()
    third = _append()

    records = decision_records.read_decision_records("p")

    assert [record["hash"] for record in records] == [first["hash"], second["hash"], third["hash"]]
    assert records[1]["prev_hash"] == records[0]["hash"]
    assert records[2]["prev_hash"] == records[1]["hash"]
    assert records[0]["prev_hash"] == ""
    # Relative, never a literal start value: the seq base belongs to audit.
    assert records[1]["seq"] == records[0]["seq"] + 1
    assert records[2]["seq"] == records[1]["seq"] + 1
    assert records[0]["schema_version"] == 2
    assert decision_records.verify_decision_chain("p") is None


def test_two_plans_have_independent_chains(plan_dir: Path) -> None:
    first_p = decision_records.append_decision_record("p", _record())
    first_q = decision_records.append_decision_record("q", _record())

    assert _chain_path(plan_dir, "p").exists()
    assert _chain_path(plan_dir, "q").exists()
    assert [r["hash"] for r in decision_records.read_decision_records("p")] == [first_p["hash"]]
    assert [r["hash"] for r in decision_records.read_decision_records("q")] == [first_q["hash"]]


# --------------------------------------------------------------------------
# Verification is read-only and reports the tampered record
# --------------------------------------------------------------------------


def test_tampered_record_is_reported_and_stays_reported(plan_dir: Path) -> None:
    _append()
    _append()
    third = _append()
    path = _chain_path(plan_dir)
    lines = _lines(path)
    target = json.loads(lines[1])
    original = path.read_bytes()

    # Flip one byte inside the second line's bytes, keeping the JSON valid.
    index = original.index(lines[1].encode())
    tampered = bytearray(original)
    tampered[index + 8] = ord("9") if tampered[index + 8] != ord("9") else ord("8")
    path.write_bytes(bytes(tampered))

    verdict = decision_records.verify_decision_chain("p")

    assert verdict == target["seq"]
    assert verdict != third["seq"]
    # Verification must not re-seal the stream: a second call reports the same
    # tamper and the bytes stay exactly as the tamper left them.
    after = path.read_bytes()
    assert decision_records.verify_decision_chain("p") == verdict
    assert path.read_bytes() == after


def test_corrupt_line_is_skipped_by_read_and_left_on_disk(plan_dir: Path) -> None:
    first = _append()
    third = _append()
    path = _chain_path(plan_dir)
    # Splice a truncated line between the two records: a mid-file corrupt line
    # must be skipped by the reader and never "repaired" on disk.
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(lines[0] + '\n{"seq": 1, "truncat\n' + lines[1] + "\n", encoding="utf-8")
    before = path.read_bytes()

    records = decision_records.read_decision_records("p")

    assert records == [first, third]
    assert path.read_bytes() == before
    assert len(_lines(path)) == 3
    assert decision_records.verify_decision_chain("p") is not None


def test_corrupt_tail_refuses_to_restart_the_chain(plan_dir: Path) -> None:
    """A garbage last line must not let the next append root a second chain."""
    first = _append()
    path = _chain_path(plan_dir)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 1, "truncat\n')
    before = path.read_bytes()

    with pytest.raises(decision_records.DecisionRecordError) as excinfo:
        _append()

    # Fail closed: nothing written, the stream is byte-identical.
    assert path.read_bytes() == before
    assert "stream" in str(excinfo.value)
    assert len(_lines(path)) == 2
    # The intact record is still readable and the damage still reported.
    assert decision_records.read_decision_records("p") == [first]
    assert decision_records.verify_decision_chain("p") is not None


# --------------------------------------------------------------------------
# Caller-supplied identity is refused, and a refused call writes nothing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field, value",
    [
        ("actor", "mallory"),
        ("principal_type", "human"),
        ("client_id", "forged-client"),
        ("seq", 99),
        ("ts", "2020-01-01T00:00:00+00:00"),
        ("hash", "0" * 64),
        ("schema_version", 3),
        ("prev_hash", "f" * 64),
        ("namespace", "other"),
    ],
)
def test_caller_supplied_server_field_is_refused(
    plan_dir: Path, field: str, value: object
) -> None:
    _append()
    _append()
    path = _chain_path(plan_dir)
    before = path.read_bytes()
    head = decision_records.read_decision_records("p")[-1]

    with pytest.raises(decision_records.DecisionRecordError) as raised:
        decision_records.append_decision_record("p", _record(**{field: value}))

    assert field in str(raised.value)
    assert path.read_bytes() == before
    assert len(_lines(path)) == 2
    # The refused call consumed no seq: the next valid append is the very next
    # record, linked to the head the refused call tried to forge.
    stored = _append()
    assert stored["seq"] == head["seq"] + 1
    assert stored["prev_hash"] == head["hash"]


# --------------------------------------------------------------------------
# Schema validation
# --------------------------------------------------------------------------


def test_unknown_field_is_refused(plan_dir: Path) -> None:
    with pytest.raises(decision_records.DecisionRecordError) as raised:
        decision_records.append_decision_record("p", _record(reasoning="because"))

    assert "reasoning" in str(raised.value)
    assert not _chain_path(plan_dir).exists()


@pytest.mark.parametrize(
    "field, value",
    [
        ("decision_id", "not-hex"),
        ("decision_id", "0f" * 15),
        ("class", "made_up"),
        ("tier", "auto+silent"),
        ("reversible", "yes"),
        ("ruling", ""),
        ("alternatives", "not-a-list"),
        ("evidence", "not-a-list"),
    ],
)
def test_malformed_field_is_refused(plan_dir: Path, field: str, value: Any) -> None:
    with pytest.raises(decision_records.DecisionRecordError) as raised:
        decision_records.append_decision_record("p", _record(**{field: value}))

    assert field in str(raised.value)
    assert not _chain_path(plan_dir).exists()


def test_subject_requires_all_four_string_fields(plan_dir: Path) -> None:
    with pytest.raises(decision_records.DecisionRecordError) as raised:
        decision_records.append_decision_record(
            "p", _record(subject={"plan": "p", "story": "s1", "pr": "42"})
        )
    assert "subject" in str(raised.value) and "sha" in str(raised.value)

    with pytest.raises(decision_records.DecisionRecordError):
        decision_records.append_decision_record(
            "p", _record(subject={"plan": "p", "story": "s1", "pr": 42, "sha": ""})
        )


def test_empty_pr_and_sha_are_allowed(plan_dir: Path) -> None:
    stored = decision_records.append_decision_record(
        "p",
        _record(subject={"plan": "p", "story": "s1", "pr": "", "sha": ""}, **{"class": "triage"}),
    )
    assert stored["subject"] == {"plan": "p", "story": "s1", "pr": "", "sha": ""}


def test_evidence_sha256_must_be_empty_or_64_hex(plan_dir: Path) -> None:
    good = _append(evidence=[{"kind": "k", "ref": "r", "sha256": "ab" * 32}])
    assert good["evidence"][0]["sha256"] == "ab" * 32
    assert _append(evidence=[{"kind": "k", "ref": "r", "sha256": ""}])["evidence"]
    assert _append(evidence=[])["evidence"] == []

    for bad in ("abc", "z" * 64, "0f" * 31, "0f" * 31 + "zz"):
        with pytest.raises(decision_records.DecisionRecordError) as raised:
            decision_records.append_decision_record(
                "p", _record(evidence=[{"kind": "k", "ref": "r", "sha256": bad}])
            )
        assert "evidence[0].sha256" in str(raised.value)
        assert bad not in str(raised.value)


def test_evidence_entry_requires_its_three_fields(plan_dir: Path) -> None:
    for entry in ({"kind": "k", "ref": "r"}, {"kind": "k", "ref": "r", "sha256": "", "x": "y"}):
        with pytest.raises(decision_records.DecisionRecordError) as raised:
            decision_records.append_decision_record("p", _record(evidence=[entry]))
        assert "evidence[0]" in str(raised.value)


# --------------------------------------------------------------------------
# Data minimisation: the field is named, never the value
# --------------------------------------------------------------------------


def test_oversized_ruling_names_the_field_only(plan_dir: Path) -> None:
    secret_value = "MARKER" + "x" * 600

    with pytest.raises(decision_records.DecisionRecordError) as raised:
        decision_records.append_decision_record("p", _record(ruling=secret_value))

    message = str(raised.value)
    assert "ruling" in message
    assert "MARKER" not in message
    assert not _chain_path(plan_dir).exists()


def test_oversized_evidence_ref_names_the_nested_field_only(plan_dir: Path) -> None:
    ref = "MARKER" + "y" * 250

    with pytest.raises(decision_records.DecisionRecordError) as raised:
        decision_records.append_decision_record(
            "p", _record(evidence=[{"kind": "k", "ref": ref, "sha256": ""}])
        )

    message = str(raised.value)
    assert "evidence[0].ref" in message
    assert "MARKER" not in message


def test_oversized_alternative_names_the_nested_field_only(plan_dir: Path) -> None:
    alternative = "MARKER" + "z" * 250

    with pytest.raises(decision_records.DecisionRecordError) as raised:
        decision_records.append_decision_record(
            "p", _record(alternatives=["fine", alternative])
        )

    message = str(raised.value)
    assert "alternatives[1]" in message
    assert "MARKER" not in message


@pytest.mark.parametrize("forbidden", ["prompt", "payload", "secret", "token", "body"])
def test_forbidden_key_at_top_level_is_refused(plan_dir: Path, forbidden: str) -> None:
    with pytest.raises(decision_records.DecisionRecordError) as raised:
        decision_records.append_decision_record("p", _record(**{forbidden: "leak"}))

    assert forbidden in str(raised.value)
    assert "leak" not in str(raised.value)


def test_forbidden_key_nested_in_evidence_is_refused(plan_dir: Path) -> None:
    with pytest.raises(decision_records.DecisionRecordError) as raised:
        decision_records.append_decision_record(
            "p",
            _record(evidence=[{"kind": "k", "ref": "r", "sha256": "", "prompt": "leak"}]),
        )

    assert "prompt" in str(raised.value)
    assert "leak" not in str(raised.value)


def test_forbidden_key_nested_in_subject_is_refused(plan_dir: Path) -> None:
    with pytest.raises(decision_records.DecisionRecordError):
        decision_records.append_decision_record(
            "p",
            _record(subject={"plan": "p", "story": "s", "pr": "", "sha": "", "secret": "x"}),
        )


def test_oversized_nested_rule_string_is_refused(plan_dir: Path) -> None:
    with pytest.raises(decision_records.DecisionRecordError) as raised:
        decision_records.append_decision_record(
            "p", _record(rule={"layer_shas": [_sha()], "persona_sha": "p" * 501,
                               "policy_sha": _sha(), "model": "m"})
        )

    assert "rule.persona_sha" in str(raised.value)


# --------------------------------------------------------------------------
# Namespace isolation
# --------------------------------------------------------------------------


def test_read_filters_by_namespace(plan_dir: Path) -> None:
    _append()
    _append()
    _append()

    assert len(decision_records.read_decision_records("p")) == 3
    assert decision_records.read_decision_records("p", namespace="other") == []


def test_records_of_another_namespace_are_invisible(plan_dir: Path) -> None:
    decision_records.append_decision_record("p", _record(), namespace="default")
    decision_records.append_decision_record("p", _record(), namespace="tenant-b")

    default_records = decision_records.read_decision_records("p")
    tenant_records = decision_records.read_decision_records("p", namespace="tenant-b")

    assert len(default_records) == 1
    assert len(tenant_records) == 1
    assert default_records[0]["namespace"] == "default"
    assert tenant_records[0]["namespace"] == "tenant-b"
    # Both landed in the one chained stream, so the chain stays verifiable.
    assert decision_records.verify_decision_chain("p") is None


# --------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------


def test_concurrent_appends_get_distinct_consecutive_seqs(plan_dir: Path) -> None:
    errors: list[decision_records.DecisionRecordError | OSError] = []

    def _worker() -> None:
        try:
            decision_records.append_decision_record("p", _record())
        except (decision_records.DecisionRecordError, OSError) as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    records = decision_records.read_decision_records("p")
    assert len(records) == 8
    seqs = [record["seq"] for record in records]
    assert len(set(seqs)) == 8
    assert seqs == list(range(seqs[0], seqs[0] + 8))
    assert decision_records.verify_decision_chain("p") is None


# --------------------------------------------------------------------------
# Anchor integration
# --------------------------------------------------------------------------


def test_anchor_covers_the_decisions_stream_and_detects_truncation(
    plan_dir: Path, anchor_file: Path
) -> None:
    for _ in range(3):
        _append()

    assert audit_anchor.anchor_heads() >= 1
    report = audit_anchor.anchor_report()
    assert report is not None and "decisions:p" in report
    assert report["decisions:p"] is None
    assert audit_anchor.verify_against_anchor("decisions:p") is None

    path = _chain_path(plan_dir)
    lines = _lines(path)
    path.write_text("\n".join(lines[:-1]) + "\n")

    verdict = audit_anchor.verify_against_anchor("decisions:p")
    assert verdict is not None
    assert "truncated" in verdict
    # Anchoring only ratchets: the truncation is not laundered by a later pass.
    audit_anchor.anchor_heads()
    assert audit_anchor.verify_against_anchor("decisions:p") == verdict


def test_plan_named_decisions_does_not_collide_with_the_prefix(plan_dir: Path) -> None:
    _append("decisions")
    _append("p")

    assert audit_anchor._stream_for("decisions") == plan_dir / "decisions.audit.jsonl"
    assert audit_anchor._stream_for("decisions:p") == _chain_path(plan_dir, "p")


# --------------------------------------------------------------------------
# Hash stability
# --------------------------------------------------------------------------


def test_hash_is_stable_across_a_read_write_round_trip(plan_dir: Path) -> None:
    stored = _append()

    read_back = decision_records.read_decision_records("p")[0]

    assert audit._record_hash(read_back) == stored["hash"]
    assert audit._canonical(read_back) == audit._canonical(stored)
    # A read must not mutate the bytes it read.
    lines_before = _chain_path(plan_dir).read_bytes()
    decision_records.read_decision_records("p")
    assert _chain_path(plan_dir).read_bytes() == lines_before


def test_validation_happens_before_any_file_is_created(plan_dir: Path) -> None:
    with pytest.raises(decision_records.DecisionRecordError):
        decision_records.append_decision_record("p", {"decision_id": "0f" * 16})

    assert not _chain_path(plan_dir).exists()


# --------------------------------------------------------------------------
# Tier is bound to the transport identity (review BLOCKING 3)
# --------------------------------------------------------------------------


def test_agent_cannot_record_a_human_tier(plan_dir: Path) -> None:
    """An agent claiming ``tier: "human"`` is repudiation in audit evidence."""
    agent = Principal(
        subject="agent/team",
        auth_method="token",
        principal_type="agent",
        team_scope="org/team",
    )
    with actor.bind_principal(agent), pytest.raises(
        decision_records.DecisionRecordError
    ) as excinfo:
        _append(tier="human")

    assert "tier" in str(excinfo.value)
    assert not _chain_path(plan_dir).exists()


def test_agent_may_record_an_autonomous_tier(plan_dir: Path) -> None:
    agent = Principal(
        subject="agent/team",
        auth_method="token",
        principal_type="agent",
        team_scope="org/team",
    )
    with actor.bind_principal(agent):
        stored = _append(tier="auto")

    assert stored["tier"] == "auto"
    assert stored["principal_type"] == "agent"


def test_unauthenticated_caller_cannot_record_a_human_tier(plan_dir: Path) -> None:
    """``ANONYMOUS`` is a Principal defaulting to ``human``; it is not one."""
    with actor.bind_principal(actor.ANONYMOUS), pytest.raises(
        decision_records.DecisionRecordError
    ) as excinfo:
        _append(tier="human")

    assert "tier" in str(excinfo.value)
    assert not _chain_path(plan_dir).exists()


# --------------------------------------------------------------------------
# The decisions: namespace is unambiguous (review BLOCKING 2)
# --------------------------------------------------------------------------


def test_colon_in_a_plan_name_is_refused(plan_dir: Path) -> None:
    """``decisions:other`` would alias the decisions chain key of plan ``other``."""
    with pytest.raises(decision_records.DecisionRecordError) as excinfo:
        _append("decisions:other")

    assert "plan" in str(excinfo.value)
    assert not _chain_path(plan_dir, "decisions:other").exists()


def test_colon_plan_audit_stream_does_not_alias_the_decisions_namespace(
    plan_dir: Path, anchor_file: Path
) -> None:
    """Two streams must stay two anchor keys, never collapse into one.

    An audit stream whose plan name carries a colon is not addressable, so it is
    skipped rather than yielded under a key that also names another plan's
    decisions chain: one of the two would otherwise never be verified and the
    surviving anchor would be pointed at the wrong file.
    """
    for _ in range(3):
        _append("p")
    # A plan name that is not a legal key, as a stray file could still produce.
    (plan_dir / "decisions:p.audit.jsonl").write_text(
        json.dumps({"seq": 0, "hash": "0" * 64}) + "\n", encoding="utf-8"
    )

    chains = dict(audit_anchor._chain_streams())

    assert list(chains).count("decisions:p") == 1
    assert chains["decisions:p"] == _chain_path(plan_dir, "p")
    assert audit_anchor._usable_chain("decisions:p") == "decisions:p"
    assert audit_anchor._usable_chain("decisions:x:y") is None
    assert audit_anchor._usable_chain("plan:audit") is None

    audit_anchor.anchor_heads()
    # The intact decisions chain is not falsely reported as tampered.
    assert audit_anchor.verify_against_anchor("decisions:p") is None
