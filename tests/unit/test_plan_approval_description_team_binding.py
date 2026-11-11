"""Re-approving what a reviewer can see: description, team and the docs.

USER DECISION 2026-11-07, second half: once the digest binds a story's
``description`` and the plan's ``team``, changing either one and re-ingesting
must reset the approval (the reviewer approved *that* text, not the new one),
``patch_story`` must keep refusing to edit ``description``, and the
documentation must say so.

Graded here:

* re-ingesting a plan whose *only* change is one story's ``description``
  resets the approval to ``pending_approval``, keeps the previous decision
  metadata and sends the ``plan_pending_approval`` notice;
* the same for a change of the plan's ``team``;
* ``patch_story(fields={"description": ...})`` is refused with the existing
  "cannot patch field(s)" error and leaves the approval record byte-identical;
* ``docs/specs/PLAN_APPROVAL.md`` §3 lists the plan-level and story-level
  digest fields and the digest version, §4 states the reset rule, §9 names
  ``description`` and ``team``;
* ``REFERENCE.md``'s ``PIPELINE_PLAN_APPROVAL`` row mentions descriptions and
  the team;
* ``CHANGELOG.md`` carries an entry naming the re-approval command.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from pipeline import ingest as ingest_mod
from pipeline import plan_approval as pa
from pipeline import server as p
from pipeline import ticketing as pt

ROOT = Path(__file__).resolve().parents[2]
DOC_PATH = ROOT / "docs" / "specs" / "PLAN_APPROVAL.md"
REFERENCE_PATH = ROOT / "REFERENCE.md"
CHANGELOG_PATH = ROOT / "CHANGELOG.md"

#: The command the upgrade notes must name, verbatim.
RESET_COMMAND = "python -m pipeline.tools.reset_outdated_approvals"

#: The plan-level fields the digest covers.
PLAN_FIELDS = ("repo_root", "role_config", "context", "team")

#: The story-level fields the digest covers.
STORY_FIELDS = (
    "summary",
    "agent_instructions",
    "dependencies",
    "persona",
    "model",
    "acceptance",
    "risk",
    "backend",
    "tdd_split",
    "files",
    "blocked_by",
    "description",
)


# --------------------------------------------------------------------------- #
# Fixtures and helpers for the ingest wiring
# --------------------------------------------------------------------------- #

@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(
        pt, "plane_request", lambda *a, **kw: pytest.fail("Plane must not be called")
    )
    return d


@pytest.fixture
def notices(monkeypatch):
    """Record the approval notices the pipeline sends."""
    sent: list[tuple[str, str, str]] = []

    def _record(plan_name, manifest, event):
        sent.append((plan_name, event, manifest.get("approval", {}).get("status")))

    monkeypatch.setattr(ingest_mod, "notify_approval_event", _record)
    return sent


def _write_plan(plan_dir: Path, tmp_path: Path, name="ap", team=None, **story_extra):
    """Write the plan source.

    ``team`` is a *plan-level* field; everything else is a story field.
    """
    story = {"summary": "Do it", "key": "S1", **story_extra}
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E", "stories": [story]}],
    }
    if team is not None:
        plan["team"] = team
    (plan_dir / f"{name}.json").write_text(json.dumps(plan))


def _manifest_path(plan_dir: Path, name="ap") -> Path:
    return plan_dir / f"{name}.manifest.json"


def _read_manifest(plan_dir: Path, name="ap") -> dict:
    return json.loads(_manifest_path(plan_dir, name).read_text())


def _approve(plan_dir: Path, name="ap") -> None:
    """Mark the ingested plan approved, as a reviewer would have."""
    manifest = _read_manifest(plan_dir, name)
    manifest["approval"] = {
        "status": "approved",
        "digest": manifest["approval"]["digest"],
        "digest_version": pa.APPROVAL_DIGEST_VERSION,
        "decided_by": "alice",
        "at": "2026-10-26T00:00:00Z",
        "role": "reviewer",
        "self_approved": False,
        "warnings_seen": ["w1"],
    }
    _manifest_path(plan_dir, name).write_text(json.dumps(manifest))


def test_changing_only_a_description_resets_the_approval(
    plan_dir, tmp_path, monkeypatch, notices
):
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")
    _write_plan(plan_dir, tmp_path, description="First description.")
    assert p.ingest_plan("ap")["ok"] is True
    _approve(plan_dir)

    _write_plan(plan_dir, tmp_path, description="Second description.")
    assert p.ingest_plan("ap")["ok"] is True

    approval = _read_manifest(plan_dir)["approval"]
    assert approval["status"] == "pending_approval"
    assert approval["digest"] == pa.canonical_digest(_read_manifest(plan_dir))
    assert approval["decided_by"] == "alice"
    assert approval["at"] == "2026-10-26T00:00:00Z"
    assert approval["warnings_seen"] == ["w1"]
    assert ("ap", "plan_pending_approval", "pending_approval") in notices


def test_changing_only_the_team_resets_the_approval(
    plan_dir, tmp_path, monkeypatch, notices
):
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")
    _write_plan(plan_dir, tmp_path, team="core")
    assert p.ingest_plan("ap")["ok"] is True
    _approve(plan_dir)

    _write_plan(plan_dir, tmp_path, team="payments")
    assert p.ingest_plan("ap")["ok"] is True

    approval = _read_manifest(plan_dir)["approval"]
    assert approval["status"] == "pending_approval"
    assert approval["digest"] == pa.canonical_digest(_read_manifest(plan_dir))
    assert approval["decided_by"] == "alice"
    assert ("ap", "plan_pending_approval", "pending_approval") in notices


def test_an_unchanged_plan_keeps_its_approval(plan_dir, tmp_path, monkeypatch):
    """The control: nothing changed, so the approval survives."""
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")
    _write_plan(plan_dir, tmp_path, description="First description.", team="core")
    assert p.ingest_plan("ap")["ok"] is True
    _approve(plan_dir)

    assert p.ingest_plan("ap")["ok"] is True

    approval = _read_manifest(plan_dir)["approval"]
    assert approval["status"] == "approved"


# --------------------------------------------------------------------------- #
# patch_story still refuses to edit a description
# --------------------------------------------------------------------------- #

class _Service:
    """The smallest thing ``_patch_story_impl`` needs.

    The patch helper authorizes through ``pipeline.authorization``, which
    reads the operator's identity; the default (local operator) may see
    everything, so no stubbing is needed here beyond the object itself.
    """

    def __init__(self) -> None:
        pass


def test_patch_story_refuses_to_edit_a_description(plan_dir, tmp_path, monkeypatch):
    from pipeline import service_story_ops

    monkeypatch.setenv(pa.APPROVAL_ENV, "on")
    _write_plan(plan_dir, tmp_path, description="First description.")
    assert p.ingest_plan("ap")["ok"] is True
    _approve(plan_dir)

    before = _manifest_path(plan_dir).read_bytes()

    result = service_story_ops._patch_story_impl(
        _Service(), "ap", "S1", {"description": "Second description."}
    )

    assert result["ok"] is False
    assert "cannot patch field(s) ['description']" in result["error"]
    assert _manifest_path(plan_dir).read_bytes() == before


# --------------------------------------------------------------------------- #
# Doc pins
# --------------------------------------------------------------------------- #

def _section(text: str, heading: str, next_heading: str) -> str:
    """Return the text between *heading* and *next_heading*."""
    assert heading in text, f"the section {heading!r} is gone from the spec"
    start = text.index(heading)
    assert next_heading in text[start:], (
        f"the section {next_heading!r} is gone from the spec"
    )
    end = text.index(next_heading, start)
    return text[start:end]


def test_the_digest_section_lists_the_plan_and_story_fields():
    text = DOC_PATH.read_text(encoding="utf-8")
    section = _section(text, "## 3. The digest", "### 3.1 Digest version")
    for field in PLAN_FIELDS:
        assert re.search(rf"(?<![\w-]){re.escape(field)}(?![\w-])", section), (
            f"the plan field {field} is not listed in section 3"
        )
    for field in STORY_FIELDS:
        assert re.search(rf"(?<![\w-]){re.escape(field)}(?![\w-])", section), (
            f"the story field {field} is not listed in section 3"
        )


def test_the_digest_section_states_the_digest_version_is_two():
    text = DOC_PATH.read_text(encoding="utf-8")
    section = _section(text, "### 3.1 Digest version", "## 4. Reset rules")
    # The version is named near the word "version" (``... version 2``,
    # ``version `2```, ``APPROVAL_DIGEST_VERSION` is 2``); the wording is the
    # implementer's, the number is not.
    assert re.search(r"(?i)version[^.\n]{0,60}\b2\b", section), (
        "the digest version is not stated as 2"
    )
    assert not re.search(r"(?i)current version[^.\n]{0,30}\b1\b", section), (
        "the spec still claims the current digest version is 1"
    )


def test_the_reset_rules_section_states_the_description_and_team_reset():
    text = DOC_PATH.read_text(encoding="utf-8")
    section = _section(text, "## 4. Reset rules", "## 5. `approve_plan`")
    assert "description" in section
    assert "team" in section
    assert "pending_approval" in section
    assert "patch_story" in section


def test_the_review_endpoint_section_names_description_and_team():
    text = DOC_PATH.read_text(encoding="utf-8")
    section = _section(text, "### The review endpoint", "## 10. Non-goals")
    assert "description" in section
    assert "team" in section


def test_the_reference_row_mentions_descriptions_and_the_team():
    text = REFERENCE_PATH.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if "PIPELINE_PLAN_APPROVAL" in line]
    assert rows, "the PIPELINE_PLAN_APPROVAL row is gone from REFERENCE.md"
    joined = "\n".join(rows)
    assert "description" in joined.lower()
    assert "team" in joined.lower()


def test_the_changelog_carries_the_upgrade_entry():
    text = CHANGELOG_PATH.read_text(encoding="utf-8")
    assert RESET_COMMAND in text, (
        f"{RESET_COMMAND!r} is not named in CHANGELOG.md"
    )
    headings = [m.start() for m in re.finditer(r"^## ", text, re.MULTILINE)]
    assert headings, "CHANGELOG.md has no release section"
    end = headings[1] if len(headings) > 1 else len(text)
    newest = text[headings[0] : end]
    assert RESET_COMMAND in newest, (
        "the re-approval command must be named in the newest changelog entry"
    )
    assert re.search(r"(?i)^### (breaking|upgrading)", newest, re.MULTILINE), (
        "the newest changelog entry has no Breaking/Upgrading section"
    )
