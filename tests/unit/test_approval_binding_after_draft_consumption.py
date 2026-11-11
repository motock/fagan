"""Approval still binds to plan-defining content once drafts are consumed.

ADR 0012 decision 5 makes the manifest the only record of a plan: a full
ingest consumes the staged draft.  That must not weaken what an approval is
bound to (ADR 0007): consuming the draft changes *where* the plan lives, not
*what* an approval covers.

These are regression guards, not a re-specification of the digest: they drive
the public API (``save_plan`` / ``ingest_plan`` / ``export_plan`` /
``patch_story`` / ``approve_plan``) and assert what each step does to the
manifest's ``approval`` record.

Graded here, with ``PIPELINE_PLAN_APPROVAL=on``:

1. approve -> export -> save -> re-ingest (unchanged) keeps the approval, with
   the same digest and the current digest version;
2. approve -> ``patch_story`` (agent_instructions) resets the approval to
   ``pending_approval`` and clears the decision metadata;
3. approve -> export -> edit one story's ``files`` -> save -> re-ingest resets
   the approval;
4. the same for a story's ``description`` alone, and (4b) for the plan's
   ``team`` alone;
4c. approve -> export -> move one story to another epic -> save -> re-ingest
   keeps the approval: the epic a story sits under is not approved content;
5. ``approve_plan`` with a stale ``expected_digest`` after a re-ingest is
   refused with ``stale_digest``;
6. with the gate off, no approval key is written at all;
7. a manifest approved under digest version 1 is not dispatched, and after
   ``reset_outdated_approvals`` plus ``approve_plan`` it is.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# pipeline.server must load before pipeline.ingest (see the import-order note
# in tests/unit/test_ingest_keyless_reingest.py).
import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import ingest as ingest_mod
from pipeline import persistence as ppers
from pipeline import plan_approval as pa
from pipeline.service import PipelineService
from pipeline.tools import reset_outdated_approvals as reset

PLAN = "ap"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    return d


@pytest.fixture
def provider(monkeypatch):
    """A ticket provider that issues no ids, so story keys come from the plan."""

    class _Provider:
        enabled = False

        def create_epic(self, summary):
            return None

        def create_story(self, summary, description, epic_id, agent):
            return None

    prov = _Provider()
    monkeypatch.setattr(p, "get_ticket_provider", lambda: prov)
    monkeypatch.setattr(ingest_mod, "_notify_user", lambda *a, **k: None)
    return prov


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _story(**over) -> dict:
    story = {
        "summary": "Do the thing",
        "key": "S1",
        "agent_instructions": "Do it carefully.",
        "description": "The original description.",
    }
    story.update(over)
    return story


def _document(tmp_path: Path, stories=None, **plan_extra) -> dict:
    document = {
        "repo_root": str(tmp_path),
        "team": "core",
        "epics": [
            {
                "summary": "E1",
                "stories": stories if stories is not None else [_story()],
            }
        ],
    }
    document.update(plan_extra)
    return document


def _stage(plan_dir: Path, document: dict, name: str = PLAN) -> None:
    result = p.save_plan(name, json.dumps(document))
    assert result["ok"] is True, result


def _manifest_path(plan_dir: Path, name: str = PLAN) -> Path:
    return plan_dir / f"{name}.manifest.json"


def _read_manifest(plan_dir: Path, name: str = PLAN) -> dict:
    return json.loads(_manifest_path(plan_dir, name).read_text())


def _approval(plan_dir: Path, name: str = PLAN) -> dict:
    return _read_manifest(plan_dir, name).get("approval", {})


def _ingest(plan_dir: Path, document: dict, name: str = PLAN) -> dict:
    """Stage ``document`` and ingest it."""
    _stage(plan_dir, document, name)
    result = p.ingest_plan(name)
    assert result["ok"] is True, result
    return result


def _approve(plan_dir: Path, name: str = PLAN) -> dict:
    """Approve the plan the way a reviewer would."""
    result = PipelineService().approve_plan(name)
    assert result["ok"] is True, result
    assert result["status"] == "approved", result
    return result


def _export(plan_dir: Path, name: str = PLAN) -> dict:
    exported = p.export_plan(name)
    assert exported["ok"] is True, exported
    assert "plan" in exported, "export_plan must return the rendered document"
    return exported["plan"]


def _stories_of(document: dict) -> list[dict]:
    return [story for epic in document["epics"] for story in epic["stories"]]


# --------------------------------------------------------------------------- #
# 1. An unchanged round trip keeps the approval.
# --------------------------------------------------------------------------- #
def test_an_unchanged_round_trip_keeps_the_approval(
    plan_dir, provider, tmp_path, gate_on
):
    _ingest(plan_dir, _document(tmp_path))
    _approve(plan_dir)
    before = _approval(plan_dir)

    assert before["status"] == "approved"
    assert before["digest_version"] == pa.APPROVAL_DIGEST_VERSION

    document = _export(plan_dir)
    _stage(plan_dir, document)
    assert p.ingest_plan(PLAN)["ok"] is True

    after = _approval(plan_dir)
    assert after["status"] == "approved", after
    assert after["digest"] == before["digest"], "the digest changed on a no-op re-ingest"
    assert after["digest_version"] == pa.APPROVAL_DIGEST_VERSION


def test_an_unchanged_round_trip_keeps_the_decision_metadata(
    plan_dir, provider, tmp_path, gate_on
):
    _ingest(plan_dir, _document(tmp_path))
    _approve(plan_dir)
    before = _approval(plan_dir)

    document = _export(plan_dir)
    _stage(plan_dir, document)
    assert p.ingest_plan(PLAN)["ok"] is True

    after = _approval(plan_dir)
    for field in ("decided_by", "at", "role"):
        assert after.get(field) == before.get(field), field


# --------------------------------------------------------------------------- #
# 2. patch_story resets the approval.
# --------------------------------------------------------------------------- #
def test_patch_story_resets_the_approval(
    plan_dir, provider, tmp_path, gate_on
):
    _ingest(plan_dir, _document(tmp_path))
    _approve(plan_dir)

    result = p.patch_story(PLAN, "S1", {"agent_instructions": "Something else."})

    assert result["ok"] is True, result
    approval = _approval(plan_dir)
    assert approval["status"] == "pending_approval", approval
    assert approval["digest"] == pa.canonical_digest(_read_manifest(plan_dir))


def test_patch_story_clears_the_decision_metadata(
    plan_dir, provider, tmp_path, gate_on
):
    _ingest(plan_dir, _document(tmp_path))
    _approve(plan_dir)
    assert _approval(plan_dir).get("decided_by")

    p.patch_story(PLAN, "S1", {"agent_instructions": "Something else."})

    approval = _approval(plan_dir)
    for field in ("decided_by", "at", "role", "self_approved"):
        assert field not in approval, f"{field} survived the reset"


# --------------------------------------------------------------------------- #
# 3. Editing a story's files resets the approval.
# --------------------------------------------------------------------------- #
def test_editing_a_storys_files_resets_the_approval(
    plan_dir, provider, tmp_path, gate_on
):
    _ingest(plan_dir, _document(tmp_path))
    _approve(plan_dir)

    document = _export(plan_dir)
    _stories_of(document)[0]["files"] = ["src/app.py"]
    _ingest(plan_dir, document)

    approval = _approval(plan_dir)
    assert approval["status"] == "pending_approval", approval
    assert approval["digest"] == pa.canonical_digest(_read_manifest(plan_dir))


def test_editing_a_storys_files_applies_the_edit(
    plan_dir, provider, tmp_path, gate_on
):
    """The control: the edit really landed, so the reset is not vacuous."""
    _ingest(plan_dir, _document(tmp_path))
    _approve(plan_dir)

    document = _export(plan_dir)
    _stories_of(document)[0]["files"] = ["src/app.py"]
    _ingest(plan_dir, document)

    assert _read_manifest(plan_dir)["stories"]["S1"]["files"] == ["src/app.py"]


# --------------------------------------------------------------------------- #
# 4. A description alone resets the approval.
# --------------------------------------------------------------------------- #
def test_editing_only_a_description_resets_the_approval(
    plan_dir, provider, tmp_path, gate_on
):
    _ingest(plan_dir, _document(tmp_path))
    _approve(plan_dir)

    document = _export(plan_dir)
    _stories_of(document)[0]["description"] = "A different description."
    _ingest(plan_dir, document)

    approval = _approval(plan_dir)
    assert approval["status"] == "pending_approval", approval
    assert approval["digest"] == pa.canonical_digest(_read_manifest(plan_dir))


def test_editing_only_a_description_applies_the_edit(
    plan_dir, provider, tmp_path, gate_on
):
    _ingest(plan_dir, _document(tmp_path))
    _approve(plan_dir)

    document = _export(plan_dir)
    _stories_of(document)[0]["description"] = "A different description."
    _ingest(plan_dir, document)

    assert (
        _read_manifest(plan_dir)["stories"]["S1"]["description"]
        == "A different description."
    )


# --------------------------------------------------------------------------- #
# 4b. The team alone resets the approval.
# --------------------------------------------------------------------------- #
def test_editing_only_the_team_resets_the_approval(
    plan_dir, provider, tmp_path, gate_on
):
    _ingest(plan_dir, _document(tmp_path))
    _approve(plan_dir)

    document = _export(plan_dir)
    document["team"] = "payments"
    _ingest(plan_dir, document)

    approval = _approval(plan_dir)
    assert approval["status"] == "pending_approval", approval
    assert approval["digest"] == pa.canonical_digest(_read_manifest(plan_dir))


def test_editing_only_the_team_applies_the_edit(
    plan_dir, provider, tmp_path, gate_on
):
    _ingest(plan_dir, _document(tmp_path))
    _approve(plan_dir)

    document = _export(plan_dir)
    document["team"] = "payments"
    _ingest(plan_dir, document)

    assert _read_manifest(plan_dir)["team"] == "payments"


# --------------------------------------------------------------------------- #
# 4c. The epic a story sits under is not approved content.
# --------------------------------------------------------------------------- #
def test_moving_a_story_to_another_epic_keeps_the_approval(
    plan_dir, provider, tmp_path, gate_on
):
    _ingest(plan_dir, _document(tmp_path))
    _approve(plan_dir)
    before = _approval(plan_dir)

    document = _export(plan_dir)
    moved = _stories_of(document)[0]
    document["epics"] = [
        {"summary": "E1", "stories": []},
        {"summary": "E2", "stories": [moved]},
    ]
    _ingest(plan_dir, document)

    approval = _approval(plan_dir)
    assert approval["status"] == "approved", approval
    assert approval["digest"] == before["digest"], "the epic placement entered the digest"


def test_moving_a_story_to_another_epic_applies_the_move(
    plan_dir, provider, tmp_path, gate_on
):
    """The control: the move really landed, so the approval is not vacuous."""
    _ingest(plan_dir, _document(tmp_path))
    _approve(plan_dir)

    document = _export(plan_dir)
    moved = _stories_of(document)[0]
    document["epics"] = [
        {"summary": "E1", "stories": []},
        {"summary": "E2", "stories": [moved]},
    ]
    _ingest(plan_dir, document)

    assert _read_manifest(plan_dir)["stories"]["S1"]["epic"] == "E2"


# --------------------------------------------------------------------------- #
# 5. A stale expected_digest is refused.
# --------------------------------------------------------------------------- #
def test_approve_plan_with_a_stale_digest_is_refused(
    plan_dir, provider, tmp_path, gate_on
):
    _ingest(plan_dir, _document(tmp_path))
    _approve(plan_dir)
    stale = _approval(plan_dir)["digest"]

    document = _export(plan_dir)
    _stories_of(document)[0]["agent_instructions"] = "Changed instructions."
    _ingest(plan_dir, document)

    result = PipelineService().approve_plan(PLAN, expected_digest=stale)

    assert result["ok"] is False, result
    assert result.get("stale_digest") is True, result
    assert _approval(plan_dir)["status"] != "approved", _approval(plan_dir)


def test_approve_plan_with_the_current_digest_is_accepted(
    plan_dir, provider, tmp_path, gate_on
):
    """The control: the refusal is about the digest, not about approving."""
    _ingest(plan_dir, _document(tmp_path))
    _approve(plan_dir)

    document = _export(plan_dir)
    _stories_of(document)[0]["agent_instructions"] = "Changed instructions."
    _ingest(plan_dir, document)

    current = pa.canonical_digest(_read_manifest(plan_dir))
    result = PipelineService().approve_plan(PLAN, expected_digest=current)

    assert result["ok"] is True, result
    assert result["status"] == "approved", result


# --------------------------------------------------------------------------- #
# 6. With the gate off, no approval key is written.
# --------------------------------------------------------------------------- #
def test_with_the_gate_off_no_approval_key_is_written(
    plan_dir, provider, tmp_path, monkeypatch
):
    monkeypatch.delenv(pa.APPROVAL_ENV, raising=False)

    _ingest(plan_dir, _document(tmp_path))

    assert "approval" not in _read_manifest(plan_dir)


def test_with_the_gate_off_a_reingest_writes_no_approval_key(
    plan_dir, provider, tmp_path, monkeypatch
):
    monkeypatch.delenv(pa.APPROVAL_ENV, raising=False)

    _ingest(plan_dir, _document(tmp_path))
    document = _export(plan_dir)
    _ingest(plan_dir, document)

    assert "approval" not in _read_manifest(plan_dir)


# --------------------------------------------------------------------------- #
# 7. An approval recorded under digest version 1 is not honoured.
# --------------------------------------------------------------------------- #
def _hand_built_manifest(plan_dir: Path, tmp_path: Path, digest_version: int) -> dict:
    """Write a manifest whose approval record claims ``digest_version``."""
    manifest = {
        "repo_root": str(tmp_path),
        "team": "core",
        "role_config": {},
        "stories": {
            "S1": {
                "summary": "Do the thing",
                "agent_instructions": "Do it carefully.",
                "description": "The original description.",
                "dependencies": [],
                "persona": None,
                "model": None,
                "acceptance": [],
                "risk": "low",
                "backend": "claude",
                "tdd_split": True,
                "files": [],
                "blocked_by": [],
                "status": "done",
                "epic": "E1",
            }
        },
    }
    manifest["approval"] = {
        "status": "approved",
        "digest": pa.canonical_digest(manifest),
        "digest_version": digest_version,
        "decided_by": "alice",
        "at": "2026-11-07T00:00:00Z",
        "role": "reviewer",
        "self_approved": False,
        "warnings_seen": [],
    }
    _manifest_path(plan_dir).write_text(json.dumps(manifest))
    return manifest


def test_a_version_one_approval_is_not_dispatched(
    plan_dir, provider, tmp_path, gate_on
):
    _hand_built_manifest(plan_dir, tmp_path, digest_version=1)

    result = p.dispatch_story(PLAN, "S1")

    assert result["ok"] is False, result
    assert "not approved" in result["error"], result


def test_a_version_one_approval_is_dispatched_after_the_reset(
    plan_dir, provider, tmp_path, gate_on
):
    _hand_built_manifest(plan_dir, tmp_path, digest_version=1)
    assert p.dispatch_story(PLAN, "S1")["ok"] is False

    outcome = reset.run()
    assert outcome.get("ok") is not False, outcome

    _approve(plan_dir)

    # The story is 'done', so the dispatch refuses it for that reason -- but
    # it got past the approval gate, which is what this guard is about.
    result = p.dispatch_story(PLAN, "S1")
    assert result["ok"] is False, result
    assert "not approved" not in result["error"], result


# --------------------------------------------------------------------------- #
# 8. The docs say so.
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "REFERENCE.md"
SPEC = ROOT / "docs" / "specs" / "PLAN_APPROVAL.md"
CHANGELOG = ROOT / "CHANGELOG.md"


def _reference_tool_lines(tool: str) -> str:
    """Return the REFERENCE.md bullet(s) that describe ``tool``."""
    text = REFERENCE.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if f"`{tool}(" in line]
    assert lines, f"the {tool} entry is gone from REFERENCE.md"
    # The bullet may wrap over several lines: take the text until the next
    # bullet at the same indentation.
    start = text.index(lines[0])
    rest = text[start + len(lines[0]) :]
    lines = [lines[0]]
    for line in rest.splitlines():
        if line.startswith(("- ", "### ")):
            break
        lines.append(line)
    return "\n".join(lines)


def _reference_plan_tools() -> str:
    """The REFERENCE.md "Draft consumption" section.

    The "MCP tools reference" section is byte-pinned against the pre-split
    README, so the draft-consumption behavior is documented beside the other
    ``ingest_plan`` manifest notes instead of inside the tool bullet.
    """
    text = REFERENCE.read_text(encoding="utf-8")
    start = text.index("### Draft consumption")
    end = text.find("\n#", start + 1)
    return text[start : end if end != -1 else None]


def test_the_reference_says_the_draft_is_consumed():
    text = _reference_plan_tools()
    assert "draft" in text.lower(), text
    assert re.search(r"(?i)draft[^\n]{0,80}(consumed|deleted|removed)", text), text


def test_the_reference_says_a_description_or_team_edit_resets_the_approval():
    text = _reference_plan_tools()
    assert "description" in text.lower(), text
    assert "team" in text.lower(), text
    assert "pending_approval" in text, text


def test_the_spec_says_the_draft_is_consumed():
    text = SPEC.read_text(encoding="utf-8")
    assert re.search(r"(?i)draft[^\n]{0,80}(consumed|deleted|removed)", text), (
        "the plan-approval spec does not say that the draft is consumed"
    )


def test_the_spec_still_says_save_plan_is_covered_by_the_digest_check():
    text = SPEC.read_text(encoding="utf-8")
    section = text[
        text.index("## 4. Reset rules") : text.index("## 5. `approve_plan`")
    ]
    assert "save_plan" in section, section
    assert "digest" in section, section


def test_the_changelog_carries_the_behaviour_change():
    text = CHANGELOG.read_text(encoding="utf-8")
    headings = [m.start() for m in re.finditer(r"^## ", text, re.MULTILINE)]
    assert headings, "CHANGELOG.md has no release section"
    end = headings[1] if len(headings) > 1 else len(text)
    newest = text[headings[0] : end]
    assert re.search(
        r"(?i)draft[^\n]{0,120}(consumed|deleted|removed)", newest
    ), "the newest changelog entry does not record the draft being consumed"
    assert re.search(r"(?i)ingest_plan", newest), (
        "the newest changelog entry does not mention ingest_plan"
    )
