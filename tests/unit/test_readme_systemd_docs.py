"""Tests for the README's systemd-unit documentation (systemd docs story).

This story makes exactly two anchored edits to README.md:

(A) in ``## Platform support``, the ``- **`launchd/*.plist`**`` bullet is
    reworded so that Linux readers are pointed at
    ``scripts/generate_systemd_units.sh`` (with a cross-reference to the
    ``## Scheduler`` section) instead of being told to run the entry points
    directly under an arbitrary init system; and

(B) in ``## Scheduler``, the trailing sentence of the
    ``### Rendering the launchd files for your machine`` subsection is trimmed
    of its "run the entry points under your own init system" advice, and a new
    H3 - ``### Rendering the systemd units for Linux`` - is appended, documenting
    ``scripts/generate_systemd_units.sh``, the per-user systemd install flow,
    ``loginctl enable-linger`` and the logrotate drop-in.

These tests deliberately assert only substrings, the presence/level/position of
the new H3, and the absence of the specific stale phrases this story removes -
never README's total contents, byte length, line count, a whole-file hash, or
the complete list of headings - because README.md is edited by many stories
over time.

The new heading must be an H3 (not an H2): a new H2 would break the
exact-H2-list assertions elsewhere in this suite.
"""

from __future__ import annotations

import re
from pathlib import Path

README_PATH = Path(__file__).resolve().parents[2] / "README.md"

GENERATOR_SCRIPT = "scripts/generate_systemd_units.sh"

LAUNCHD_H3 = "Rendering the launchd files for your machine"
SYSTEMD_H3 = "Rendering the systemd units for Linux"

# The exact stale phrases this story removes. Each is dash-free so the
# assertion does not depend on whether the implementer writes an em-dash or a
# hyphen.
STALE_PLATFORM_PHRASE = "run the same entry points directly"
STALE_PLATFORM_INIT_PHRASE = "under your init system or"
STALE_PLATFORM_SUPERVISOR_PHRASE = "supervisor of choice"
STALE_SCHEDULER_PHRASE = "on Linux, run the entry points under your own init system instead."

# The dash-free fragment of the sentence EDIT 2 must keep (it only trims the
# trailing Linux advice, it must not delete the whole sentence).
KEPT_MACOS_ONLY_FRAGMENT = "These launchd files are macOS-only"

PLATFORM_ANCHOR = "[Platform support](#platform-support)"
SCHEDULER_ANCHOR = "[Scheduler](#scheduler)"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


# --- helpers ----------------------------------------------------------------


def _readme_text() -> str:
    assert README_PATH.exists(), f"README.md not found at {README_PATH}"
    return README_PATH.read_text(encoding="utf-8")


def _headings(text: str) -> list[tuple[int, str]]:
    """Return ``(level, title)`` for every ATX heading in *text*."""
    out: list[tuple[int, str]] = []
    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            out.append((len(match.group(1)), match.group(2)))
    return out


def _heading_line_index(text: str, title: str) -> int:
    """Index of the line whose heading text is exactly *title*."""
    for index, line in enumerate(text.splitlines()):
        match = _HEADING_RE.match(line)
        if match and match.group(2) == title:
            return index
    raise AssertionError(f"README.md has no heading titled {title!r}")


def _section_body(text: str, title: str, max_level: int = 3) -> str:
    """Body of the heading *title*, up to the next heading of level <= max_level."""
    lines = text.splitlines()
    start = _heading_line_index(text, title)
    body: list[str] = []
    for line in lines[start + 1 :]:
        match = _HEADING_RE.match(line)
        if match and len(match.group(1)) <= max_level:
            break
        body.append(line)
    return "\n".join(body)


def _bullet_paragraph(text: str, prefix: str) -> str:
    """The full (possibly wrapped) bullet whose first line starts with *prefix*."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            block = [line]
            for following in lines[index + 1 :]:
                if not following.strip() or following.lstrip().startswith("- "):
                    break
                block.append(following)
            return "\n".join(block)
    raise AssertionError(f"README.md has no bullet starting with {prefix!r}")


# --- (1) the generator script is referenced --------------------------------


def test_readme_mentions_systemd_generator_script() -> None:
    assert GENERATOR_SCRIPT in _readme_text(), (
        "README.md does not mention scripts/generate_systemd_units.sh; the "
        "Linux/systemd rendering path must be documented."
    )


# --- (2) the new H3 exists, is an H3, and sits in the Scheduler section -----


def test_readme_has_systemd_h3_heading() -> None:
    headings = _headings(_readme_text())
    assert (3, SYSTEMD_H3) in headings, (
        f"README.md has no H3 heading titled {SYSTEMD_H3!r}; found H3s: "
        f"{[title for level, title in headings if level == 3]}"
    )


def test_systemd_heading_is_not_an_h2() -> None:
    headings = _headings(_readme_text())
    assert (2, SYSTEMD_H3) not in headings, (
        f"{SYSTEMD_H3!r} must be an H3, not an H2 - a new H2 breaks the "
        "exact-H2-list assertions elsewhere in this suite."
    )


def test_systemd_h3_lives_inside_scheduler_section() -> None:
    text = _readme_text()
    lines = text.splitlines()
    scheduler = _heading_line_index(text, "Scheduler")
    systemd = _heading_line_index(text, SYSTEMD_H3)
    next_h2 = next(
        (
            index
            for index, line in enumerate(lines)
            if index > scheduler and re.match(r"^##\s+", line)
        ),
        len(lines),
    )
    assert scheduler < systemd < next_h2, (
        f"{SYSTEMD_H3!r} must appear inside the `## Scheduler` section "
        f"(after line {scheduler}, before the next H2 at line {next_h2}); "
        f"it is at line {systemd}."
    )


def test_systemd_h3_follows_the_launchd_h3() -> None:
    text = _readme_text()
    launchd = _heading_line_index(text, LAUNCHD_H3)
    systemd = _heading_line_index(text, SYSTEMD_H3)
    assert launchd < systemd, (
        f"{SYSTEMD_H3!r} should be documented after {LAUNCHD_H3!r}."
    )


def test_launchd_h3_heading_still_present() -> None:
    assert (3, LAUNCHD_H3) in _headings(_readme_text()), (
        f"the pre-existing H3 {LAUNCHD_H3!r} must not be removed or renamed."
    )


# --- (3) loginctl enable-linger is documented ------------------------------


def test_readme_mentions_loginctl_enable_linger() -> None:
    assert "loginctl enable-linger" in _readme_text(), (
        "README.md does not document `loginctl enable-linger` for the "
        "per-user systemd units."
    )


# --- (4) the macOS-only sentence survives (EDIT 2 trimmed, not deleted) -----


def test_readme_keeps_macos_only_sentence() -> None:
    assert KEPT_MACOS_ONLY_FRAGMENT in _readme_text(), (
        f"README.md no longer contains {KEPT_MACOS_ONLY_FRAGMENT!r}; EDIT 2 "
        "must trim the trailing Linux advice, not delete the sentence."
    )


def test_macos_only_sentence_precedes_systemd_h3() -> None:
    text = _readme_text()
    lines = text.splitlines()
    kept = next(
        (index for index, line in enumerate(lines) if KEPT_MACOS_ONLY_FRAGMENT in line),
        None,
    )
    assert kept is not None, f"missing {KEPT_MACOS_ONLY_FRAGMENT!r}"
    systemd = _heading_line_index(text, SYSTEMD_H3)
    assert kept < systemd, (
        f"{KEPT_MACOS_ONLY_FRAGMENT!r} should immediately precede the new "
        f"{SYSTEMD_H3!r} heading."
    )


def test_launchd_section_keeps_platform_support_anchor() -> None:
    body = _section_body(_readme_text(), LAUNCHD_H3)
    assert PLATFORM_ANCHOR in body, (
        f"the {LAUNCHD_H3!r} subsection must keep its "
        f"{PLATFORM_ANCHOR!r} cross-reference."
    )


# --- (5) EDIT 1 landed: the launchd/*.plist bullet was reworded -------------


def test_launchd_plist_bullet_dropped_old_phrase() -> None:
    paragraph = _bullet_paragraph(_readme_text(), "- **`launchd/*.plist`**")
    assert STALE_PLATFORM_PHRASE not in paragraph, (
        f"the `launchd/*.plist` bullet still contains {STALE_PLATFORM_PHRASE!r}; "
        "EDIT 1 must replace that wording, not append text elsewhere."
    )


def test_launchd_plist_bullet_dropped_old_init_system_wording() -> None:
    paragraph = _bullet_paragraph(_readme_text(), "- **`launchd/*.plist`**")
    for stale in (STALE_PLATFORM_INIT_PHRASE, STALE_PLATFORM_SUPERVISOR_PHRASE):
        assert stale not in paragraph, (
            f"the `launchd/*.plist` bullet still contains {stale!r}."
        )


def test_launchd_plist_bullet_points_at_systemd_generator() -> None:
    paragraph = _bullet_paragraph(_readme_text(), "- **`launchd/*.plist`**")
    assert GENERATOR_SCRIPT in paragraph, (
        f"the `launchd/*.plist` bullet must point Linux readers at "
        f"{GENERATOR_SCRIPT!r}."
    )
    assert SCHEDULER_ANCHOR in paragraph, (
        f"the `launchd/*.plist` bullet must cross-reference {SCHEDULER_ANCHOR!r}."
    )


def test_launchd_plist_bullet_keeps_macos_framing() -> None:
    paragraph = _bullet_paragraph(_readme_text(), "- **`launchd/*.plist`**")
    assert "packaged as launchd jobs on macOS" in paragraph, (
        "the `launchd/*.plist` bullet must keep its macOS framing."
    )
    assert "foreground terminal/`tmux` session" in paragraph, (
        "the `launchd/*.plist` bullet must keep the foreground/tmux fallback."
    )


# --- EDIT 2 landed: the stale Scheduler sentence was trimmed ----------------


def test_scheduler_section_dropped_old_linux_advice() -> None:
    assert STALE_SCHEDULER_PHRASE not in _readme_text(), (
        f"README.md still contains {STALE_SCHEDULER_PHRASE!r}; EDIT 2 must "
        "trim that trailing clause."
    )


# --- the new systemd subsection documents the rendering + install flow ------


def test_systemd_section_documents_templates_and_launchd_parity() -> None:
    body = _section_body(_readme_text(), SYSTEMD_H3)
    assert "systemd/*.template" in body, (
        "the systemd subsection must name the `systemd/*.template` sources."
    )
    assert "scripts/generate_launchd_plists.sh" in body, (
        "the systemd subsection must reference the launchd generator it mirrors."
    )
    assert "MLX" in body, (
        "the systemd subsection must note that MLX is Apple Silicon-only."
    )


def test_systemd_section_documents_generator_invocation() -> None:
    body = _section_body(_readme_text(), SYSTEMD_H3)
    assert "--repo-root" in body and "--out-dir" in body, (
        "the systemd subsection must show the generator's --repo-root/--out-dir "
        "invocation."
    )
    assert "$HOME/fagan" in body, (
        "the systemd subsection must show the example --repo-root value."
    )


def test_systemd_section_documents_per_user_install() -> None:
    body = _section_body(_readme_text(), SYSTEMD_H3)
    for expected in (
        "mkdir -p ~/.config/systemd/user",
        "systemctl --user daemon-reload",
        "systemctl --user enable --now com.fagan.pipeline.advance-scheduler.service",
        "systemctl --user enable --now com.fagan.pipeline.usage-poller.timer",
        'loginctl enable-linger "$USER"',
    ):
        assert expected in body, (
            f"the systemd subsection must document {expected!r}."
        )


def test_systemd_section_documents_logrotate_drop_in() -> None:
    body = _section_body(_readme_text(), SYSTEMD_H3)
    assert "systemd/pipeline-logs.logrotate.conf" in body, (
        "the systemd subsection must name the rendered logrotate conf."
    )
    assert "/etc/logrotate.d/com.fagan.pipeline" in body, (
        "the systemd subsection must show the logrotate drop-in destination."
    )
