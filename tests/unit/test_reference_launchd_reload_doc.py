"""Tests for the REFERENCE.md doc edit documenting the launchd install + reload
requirement for the advance-scheduler daemon.

Docs-only story adding a short subsection next to the existing launchd /
EnvironmentVariables material. It must state, in prose:

* ``launchd/*.plist(.template)`` in the repo is a TEMPLATE; the running daemon
  reads only the INSTALLED agent under ``~/Library/LaunchAgents/``.
* the procedure: edit the installed plist surgically, then reload with
  ``scripts/reload_pipeline_daemon.sh`` (equivalently ``launchctl unload`` then
  ``launchctl load`` on that path) — and that the change has NO effect until
  this reload happens.
* the drift warning: the installed agent can differ from the repo copy (this
  machine's installed copy pins ``PIPELINE_LOCAL_NUM_CTX=32768`` and sets
  ``PIPELINE_AUTO_TRIAGE`` and ``PIPELINE_MAX_CONCURRENT_AGENTS=4``, none of
  which the repo template says), so a wholesale regenerate-and-install silently
  drops local overrides.

``REFERENCE.md`` is a shared, cumulative artifact that sibling stories also
edit, so these tests locate the new subsection by unique text (the
``scripts/reload_pipeline_daemon.sh`` mention) and assert only that
subsection's content — never the file's total contents or line count.

These tests are RED until the doc edit lands (the implementation dispatch),
which is the intended state.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REFERENCE_PATH = REPO_ROOT / "REFERENCE.md"

# The unique text that anchors the new subsection.
_ANCHOR = "scripts/reload_pipeline_daemon.sh"

# The installed-agent path the subsection must name.
_INSTALLED_DIR = "~/Library/LaunchAgents/"
_INSTALLED_PLIST = "com.fagan.pipeline.advance-scheduler.plist"

# The Configuration section heading; the new material lives next to it.
_CONFIG_HEADING = "## Configuration (environment variables)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reference_text() -> str:
    assert REFERENCE_PATH.exists(), f"REFERENCE.md missing: {REFERENCE_PATH}"
    return REFERENCE_PATH.read_text(encoding="utf-8")


def _heading_level(line: str) -> int | None:
    stripped = line.lstrip()
    if not stripped.startswith("#"):
        return None
    hashes = len(stripped) - len(stripped.lstrip("#"))
    if hashes == 0 or not stripped[hashes:hashes + 1].isspace():
        return None
    return hashes


def _subsection_containing(text: str, anchor: str) -> str:
    """Return the markdown section (heading + body) that contains ``anchor``.

    The section runs from the nearest heading at or before the anchor up to the
    next heading of the same or higher level. This locates the new subsection
    by unique text, never by line number.
    """
    lines = text.splitlines()
    anchor_idx = next(
        (i for i, line in enumerate(lines) if anchor in line), None
    )
    assert anchor_idx is not None, (
        f"REFERENCE.md must mention {anchor!r} in the new subsection"
    )

    start = 0
    start_level = 1
    for i in range(anchor_idx, -1, -1):
        level = _heading_level(lines[i])
        if level is not None:
            start = i
            start_level = level
            break

    end = len(lines)
    for i in range(start + 1, len(lines)):
        level = _heading_level(lines[i])
        if level is not None and level <= start_level:
            end = i
            break

    return "\n".join(lines[start:end])


# ---------------------------------------------------------------------------
# Anchor existence
# ---------------------------------------------------------------------------

class TestSubsectionExists:
    def test_reference_file_exists(self):
        _reference_text()

    def test_new_subsection_mentions_the_reload_script(self):
        text = _reference_text()
        assert _ANCHOR in text, (
            f"REFERENCE.md must document {_ANCHOR} in the new subsection"
        )

    def test_subsection_is_located(self):
        _subsection_containing(_reference_text(), _ANCHOR)  # asserts locatable

    def test_subsection_sits_after_the_configuration_heading(self):
        """The new material belongs next to the launchd / EnvironmentVariables
        material, i.e. after the Configuration section heading."""
        text = _reference_text()
        assert _CONFIG_HEADING in text, (
            f"expected the existing heading {_CONFIG_HEADING!r} in REFERENCE.md"
        )
        assert text.index(_CONFIG_HEADING) < text.index(_ANCHOR), (
            "the reload subsection must appear after the Configuration "
            "(environment variables) heading"
        )


# ---------------------------------------------------------------------------
# Template vs installed agent
# ---------------------------------------------------------------------------

class TestTemplateVsInstalled:
    def test_names_the_installed_launchagents_path(self):
        section = _subsection_containing(_reference_text(), _ANCHOR)
        assert _INSTALLED_DIR in section, (
            "subsection must name the installed agent directory "
            f"{_INSTALLED_DIR!r}"
        )

    def test_names_the_installed_plist(self):
        section = _subsection_containing(_reference_text(), _ANCHOR)
        assert _INSTALLED_PLIST in section, (
            f"subsection must name the installed plist {_INSTALLED_PLIST!r}"
        )

    def test_states_repo_plist_is_a_template(self):
        section = _subsection_containing(_reference_text(), _ANCHOR)
        assert "template" in section.lower(), (
            "subsection must state that launchd/*.plist(.template) in the repo "
            "is a TEMPLATE"
        )

    def test_states_daemon_reads_only_the_installed_agent(self):
        section = _subsection_containing(_reference_text(), _ANCHOR).lower()
        assert "installed" in section, (
            "subsection must state the running daemon reads only the installed "
            "agent"
        )
        assert "reads" in section or "read" in section, (
            "subsection must state what the running daemon reads"
        )


# ---------------------------------------------------------------------------
# The procedure: surgical edit + reload
# ---------------------------------------------------------------------------

class TestProcedure:
    def test_names_the_reload_script(self):
        section = _subsection_containing(_reference_text(), _ANCHOR)
        assert _ANCHOR in section, (
            f"subsection must name {_ANCHOR!r} as the reload helper"
        )

    def test_names_launchctl_unload(self):
        section = _subsection_containing(_reference_text(), _ANCHOR)
        assert "launchctl unload" in section, (
            "subsection must name `launchctl unload`"
        )

    def test_names_launchctl_load(self):
        section = _subsection_containing(_reference_text(), _ANCHOR)
        assert "launchctl load" in section, (
            "subsection must name `launchctl load`"
        )

    def test_states_edit_the_installed_plist_surgically(self):
        section = _subsection_containing(_reference_text(), _ANCHOR).lower()
        assert "surgical" in section or "surgically" in section, (
            "subsection must say to edit the installed plist surgically"
        )

    def test_states_change_has_no_effect_until_reload(self):
        section = _subsection_containing(_reference_text(), _ANCHOR).lower()
        assert "no effect" in section, (
            "subsection must state the change has NO effect until the reload "
            "happens"
        )
        assert "reload" in section, (
            "subsection must state the reload is what applies the change"
        )


# ---------------------------------------------------------------------------
# The drift warning
# ---------------------------------------------------------------------------

class TestDriftWarning:
    def test_warns_about_drift(self):
        section = _subsection_containing(_reference_text(), _ANCHOR).lower()
        assert "drift" in section, (
            "subsection must warn that the installed agent can drift from the "
            "repo copy"
        )

    def test_names_concrete_drifted_num_ctx_value(self):
        section = _subsection_containing(_reference_text(), _ANCHOR)
        assert "PIPELINE_LOCAL_NUM_CTX=32768" in section, (
            "subsection must name the concrete drifted value "
            "PIPELINE_LOCAL_NUM_CTX=32768"
        )

    def test_names_concrete_drifted_auto_triage(self):
        section = _subsection_containing(_reference_text(), _ANCHOR)
        assert "PIPELINE_AUTO_TRIAGE" in section, (
            "subsection must name the concrete drifted PIPELINE_AUTO_TRIAGE"
        )

    def test_names_concrete_drifted_max_concurrent_agents(self):
        section = _subsection_containing(_reference_text(), _ANCHOR)
        assert "PIPELINE_MAX_CONCURRENT_AGENTS=4" in section, (
            "subsection must name the concrete drifted value "
            "PIPELINE_MAX_CONCURRENT_AGENTS=4"
        )

    def test_warns_wholesale_regenerate_drops_local_overrides(self):
        section = _subsection_containing(_reference_text(), _ANCHOR).lower()
        assert "regenerate" in section, (
            "subsection must warn about a wholesale regenerate-and-install"
        )
        assert "override" in section, (
            "subsection must state that a wholesale regenerate-and-install "
            "silently drops local overrides"
        )
