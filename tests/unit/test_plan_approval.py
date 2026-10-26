"""Plan-approval state and content digest (ENT-LC-01).

``pipeline/plan_approval.py`` owns the opt-in approval gate:

* ``approval_enabled()`` parses ``PIPELINE_PLAN_APPROVAL`` fail-closed.
* ``canonical_digest()`` hashes only the *authored* content of a manifest, so
  an approval decision (which is not authored content) never changes it.
* ``is_activatable()`` is always true while the gate is disabled, and only
  true for an ``approved`` record while it is enabled.
* ``on_ingest()`` recomputes the digest and keeps ``approved`` only when the
  previous record was approved *and* its digest still matches.

The ingest wiring is pinned here too: with the gate disabled the written
manifest must be byte-identical to today's (no ``approval`` key added,
modified or removed); with it enabled the record is written before the
manifest is persisted.
"""
import json

import pytest

from pipeline import ingest as ingest_mod
from pipeline import plan_approval as pa
from pipeline import server as p
from pipeline import ticketing as pt

# ---------------------------------------------------------------------------
# approval_enabled()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "off", "0", "false"])
def test_should_report_disabled_for_falsey_flag_values(monkeypatch, value):
    monkeypatch.setenv(pa.APPROVAL_ENV, value)

    assert pa.approval_enabled() is False


def test_should_report_disabled_when_flag_is_unset(monkeypatch):
    monkeypatch.delenv(pa.APPROVAL_ENV, raising=False)

    assert pa.approval_enabled() is False


@pytest.mark.parametrize("value", ["on", "1", "true"])
def test_should_report_enabled_for_truthy_flag_values(monkeypatch, value):
    monkeypatch.setenv(pa.APPROVAL_ENV, value)

    assert pa.approval_enabled() is True


@pytest.mark.parametrize("value", ["yes", "ON", "True", "2", "enabled"])
def test_should_raise_naming_variable_and_value_for_unknown_flag(monkeypatch, value):
    monkeypatch.setenv(pa.APPROVAL_ENV, value)

    with pytest.raises(ValueError) as excinfo:
        pa.approval_enabled()

    message = str(excinfo.value)
    assert "PIPELINE_PLAN_APPROVAL" in message
    assert value in message


def test_should_read_the_flag_on_every_call(monkeypatch):
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")
    assert pa.approval_enabled() is True

    monkeypatch.setenv(pa.APPROVAL_ENV, "off")
    assert pa.approval_enabled() is False


# ---------------------------------------------------------------------------
# canonical_digest()
# ---------------------------------------------------------------------------


def _manifest(**overrides):
    manifest = {
        "repo_root": "/repo",
        "role_config": {"dispatch": "claude"},
        "context": {"org": "acme"},
        "stories": {
            "S1": {
                "summary": "Do it",
                "agent_instructions": "A",
                "dependencies": [],
                "persona": "backend",
                "model": "claude",
                "acceptance": ["it works"],
                "risk": "low",
                "backend": "claude",
                "tdd_split": False,
                "files": ["pipeline/x.py"],
                "blocked_by": [],
            }
        },
    }
    manifest.update(overrides)
    return manifest


def test_should_be_stable_across_dict_insertion_order():
    first = _manifest()
    second = {
        "stories": {
            "S1": {
                "blocked_by": [],
                "files": ["pipeline/x.py"],
                "tdd_split": False,
                "backend": "claude",
                "risk": "low",
                "acceptance": ["it works"],
                "model": "claude",
                "persona": "backend",
                "dependencies": [],
                "agent_instructions": "A",
                "summary": "Do it",
            }
        },
        "context": {"org": "acme"},
        "role_config": {"dispatch": "claude"},
        "repo_root": "/repo",
    }

    assert pa.canonical_digest(first) == pa.canonical_digest(second)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda m: m["stories"]["S1"].__setitem__("agent_instructions", "B"),
            id="agent_instructions",
        ),
        pytest.param(
            lambda m: m["stories"]["S1"].__setitem__("files", ["pipeline/y.py"]),
            id="files",
        ),
        pytest.param(
            lambda m: m.__setitem__("role_config", {"dispatch": "local"}),
            id="role_config",
        ),
    ],
)
def test_should_change_when_an_authored_field_changes(mutate):
    baseline = pa.canonical_digest(_manifest())
    changed = _manifest()
    mutate(changed)

    assert pa.canonical_digest(changed) != baseline


def test_should_not_change_when_only_approval_changes():
    pending = _manifest(approval={"status": "pending_approval", "digest": "d1"})
    approved = _manifest(
        approval={
            "status": "approved",
            "digest": "d1",
            "decided_by": "alice",
            "decided_at": "2026-10-26T00:00:00Z",
            "role": "reviewer",
            "self_approved": False,
            "warnings_seen": ["w1"],
        }
    )

    assert pa.canonical_digest(pending) == pa.canonical_digest(approved)


def test_should_not_mutate_the_manifest():
    manifest = _manifest()
    before = json.dumps(manifest, sort_keys=True)

    pa.canonical_digest(manifest)

    assert json.dumps(manifest, sort_keys=True) == before


# ---------------------------------------------------------------------------
# is_activatable()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "approval",
    [None, "not-a-dict", "approved", {"status": "pending_approval"}, {}],
)
def test_should_be_activatable_when_disabled_even_with_malformed_approval(
    monkeypatch, approval
):
    monkeypatch.setenv(pa.APPROVAL_ENV, "off")
    manifest = _manifest()
    if approval is not None:
        manifest["approval"] = approval

    assert pa.is_activatable(manifest) is True


def test_should_be_activatable_when_enabled_and_approved(monkeypatch):
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")

    assert pa.is_activatable(_manifest(approval={"status": "approved"})) is True


@pytest.mark.parametrize(
    "approval",
    [None, "approved", {"status": "pending_approval"}, {"status": "rejected"}, {}],
)
def test_should_not_be_activatable_when_enabled_and_not_approved(
    monkeypatch, approval
):
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")
    manifest = _manifest()
    if approval is not None:
        manifest["approval"] = approval

    assert pa.is_activatable(manifest) is False


# ---------------------------------------------------------------------------
# on_ingest()
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_digest(monkeypatch):
    """Digest keyed off ``agent_instructions`` so content changes are explicit."""

    def _digest(manifest):
        instructions = manifest["stories"]["S1"]["agent_instructions"]
        return {"A": "d1", "B": "d2"}[instructions]

    monkeypatch.setattr(pa, "canonical_digest", _digest)
    return _digest


def test_should_initialise_a_pending_record_with_digest_and_empty_warnings(
    stub_digest,
):
    record = pa.on_ingest(_manifest())

    assert record["status"] == "pending_approval"
    assert record["digest"] == "d1"
    assert record["warnings_seen"] == []


def test_should_keep_an_approved_record_whose_digest_matches(stub_digest):
    manifest = _manifest(
        approval={
            "status": "approved",
            "digest": "d1",
            "decided_by": "alice",
            "decided_at": "2026-10-26T00:00:00Z",
            "role": "reviewer",
            "self_approved": False,
            "warnings_seen": ["w1"],
        }
    )

    record = pa.on_ingest(manifest)

    assert record["status"] == "approved"
    assert record["digest"] == "d1"
    assert record["decided_by"] == "alice"
    assert record["decided_at"] == "2026-10-26T00:00:00Z"
    assert record["role"] == "reviewer"
    assert record["self_approved"] is False
    assert record["warnings_seen"] == ["w1"]


def test_should_reset_to_pending_on_changed_content_but_preserve_decision_fields(
    stub_digest,
):
    manifest = _manifest(
        approval={
            "status": "approved",
            "digest": "d1",
            "decided_by": "alice",
            "decided_at": "2026-10-26T00:00:00Z",
            "role": "reviewer",
            "self_approved": False,
            "warnings_seen": ["w1"],
        }
    )
    manifest["stories"]["S1"]["agent_instructions"] = "B"

    record = pa.on_ingest(manifest)

    assert record["status"] == "pending_approval"
    assert record["digest"] == "d2"
    assert record["decided_by"] == "alice"
    assert record["decided_at"] == "2026-10-26T00:00:00Z"
    assert record["role"] == "reviewer"
    assert record["self_approved"] is False
    assert record["warnings_seen"] == ["w1"]


def test_should_stay_pending_on_a_follow_up_reingest_of_changed_content(stub_digest):
    manifest = _manifest(
        approval={
            "status": "approved",
            "digest": "d1",
            "decided_by": "alice",
            "decided_at": "2026-10-26T00:00:00Z",
            "role": "reviewer",
            "self_approved": False,
            "warnings_seen": ["w1"],
        }
    )
    manifest["stories"]["S1"]["agent_instructions"] = "B"
    first = pa.on_ingest(manifest)

    manifest["approval"] = first
    second = pa.on_ingest(manifest)

    assert second["status"] == "pending_approval"
    assert second["digest"] == "d2"
    assert second["decided_by"] == "alice"
    assert second["decided_at"] == "2026-10-26T00:00:00Z"
    assert second["role"] == "reviewer"
    assert second["self_approved"] is False
    assert second["warnings_seen"] == ["w1"]


def test_should_treat_a_non_dict_approval_as_empty(stub_digest):
    record = pa.on_ingest(_manifest(approval="approved"))

    assert record["status"] == "pending_approval"
    assert record["digest"] == "d1"
    assert record["warnings_seen"] == []


def test_should_not_mutate_the_manifest_on_ingest(stub_digest):
    manifest = _manifest()
    before = json.dumps(manifest, sort_keys=True)

    pa.on_ingest(manifest)

    assert json.dumps(manifest, sort_keys=True) == before


# ---------------------------------------------------------------------------
# ingest wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(
        pt, "plane_request", lambda *a, **kw: pytest.fail("Plane must not be called")
    )
    return d


def _write_plan(plan_dir, tmp_path, name="ap", **story_extra):
    story = {"summary": "Do it", "key": "S1", **story_extra}
    plan = {"repo_root": str(tmp_path), "epics": [{"summary": "E", "stories": [story]}]}
    (plan_dir / f"{name}.json").write_text(json.dumps(plan))


def _manifest_path(plan_dir, name="ap"):
    return plan_dir / f"{name}.manifest.json"


def _read_manifest(plan_dir, name="ap"):
    return json.loads(_manifest_path(plan_dir, name).read_text())


def test_should_not_touch_approval_when_disabled(plan_dir, tmp_path, monkeypatch):
    monkeypatch.delenv(pa.APPROVAL_ENV, raising=False)
    monkeypatch.setattr(
        ingest_mod,
        "on_ingest",
        lambda manifest: pytest.fail("on_ingest must not run while disabled"),
    )
    _write_plan(plan_dir, tmp_path)

    result = p.ingest_plan("ap")

    assert result["ok"] is True
    assert "approval" not in _read_manifest(plan_dir)


def test_should_write_a_byte_identical_manifest_when_disabled(
    plan_dir, tmp_path, monkeypatch
):
    _write_plan(plan_dir, tmp_path)

    monkeypatch.delenv(pa.APPROVAL_ENV, raising=False)
    assert p.ingest_plan("ap")["ok"] is True
    unset_bytes = _manifest_path(plan_dir).read_bytes()

    _manifest_path(plan_dir).unlink()
    monkeypatch.setenv(pa.APPROVAL_ENV, "off")
    assert p.ingest_plan("ap")["ok"] is True
    off_bytes = _manifest_path(plan_dir).read_bytes()

    assert off_bytes == unset_bytes


def test_should_leave_an_existing_approval_key_untouched_when_disabled(
    plan_dir, tmp_path, monkeypatch
):
    monkeypatch.delenv(pa.APPROVAL_ENV, raising=False)
    _write_plan(plan_dir, tmp_path)
    assert p.ingest_plan("ap")["ok"] is True

    existing = {"status": "approved", "digest": "old"}
    manifest = _read_manifest(plan_dir)
    manifest["approval"] = existing
    _manifest_path(plan_dir).write_text(json.dumps(manifest))

    assert p.ingest_plan("ap")["ok"] is True

    assert _read_manifest(plan_dir)["approval"] == existing


def test_should_write_pending_approval_on_first_enabled_ingest(
    plan_dir, tmp_path, monkeypatch
):
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")
    _write_plan(plan_dir, tmp_path)

    assert p.ingest_plan("ap")["ok"] is True

    approval = _read_manifest(plan_dir)["approval"]
    assert approval["status"] == "pending_approval"
    assert approval["digest"] == pa.canonical_digest(_read_manifest(plan_dir))
    assert approval["warnings_seen"] == []


def test_should_preserve_an_approved_matching_record_across_reingest(
    plan_dir, tmp_path, monkeypatch
):
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")
    _write_plan(plan_dir, tmp_path)
    assert p.ingest_plan("ap")["ok"] is True

    manifest = _read_manifest(plan_dir)
    manifest["approval"] = {
        "status": "approved",
        "digest": manifest["approval"]["digest"],
        "decided_by": "alice",
        "decided_at": "2026-10-26T00:00:00Z",
        "role": "reviewer",
        "self_approved": False,
        "warnings_seen": ["w1"],
    }
    _manifest_path(plan_dir).write_text(json.dumps(manifest))

    assert p.ingest_plan("ap")["ok"] is True

    approval = _read_manifest(plan_dir)["approval"]
    assert approval["status"] == "approved"
    assert approval["decided_by"] == "alice"
    assert approval["warnings_seen"] == ["w1"]


def test_should_reset_to_pending_when_authored_content_changes(
    plan_dir, tmp_path, monkeypatch
):
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")
    _write_plan(plan_dir, tmp_path, agent_instructions="A")
    assert p.ingest_plan("ap")["ok"] is True

    manifest = _read_manifest(plan_dir)
    manifest["approval"] = {
        "status": "approved",
        "digest": manifest["approval"]["digest"],
        "decided_by": "alice",
        "decided_at": "2026-10-26T00:00:00Z",
        "role": "reviewer",
        "self_approved": False,
        "warnings_seen": ["w1"],
    }
    _manifest_path(plan_dir).write_text(json.dumps(manifest))

    _write_plan(plan_dir, tmp_path, agent_instructions="B")
    assert p.ingest_plan("ap")["ok"] is True

    approval = _read_manifest(plan_dir)["approval"]
    assert approval["status"] == "pending_approval"
    assert approval["digest"] == pa.canonical_digest(_read_manifest(plan_dir))
    assert approval["decided_by"] == "alice"
    assert approval["warnings_seen"] == ["w1"]
