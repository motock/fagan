"""Tests for the README's launchd-generator documentation (PUB-04).

This story makes exactly two additions to README.md:

(A) a new H3 under the existing ``## Scheduler`` H2 explaining that the
committed ``launchd/*.plist`` and ``launchd/pipeline-logs.newsyslog.conf``
files carry the maintainer's own absolute paths and are checked in only as a
reference copy, that a fresh install should regenerate them with
``scripts/generate_launchd_plists.sh`` (documenting the script's
``--repo-root`` / ``--out-dir`` / ``--mlx-model-path`` flags, the
``MLX_MODEL_PATH`` env-var alternative, the fail-closed behaviour when
neither is supplied, and that the newsyslog conf is rendered by the same
script), plus a cross-reference to the macOS-only note in
``## Platform support``; and

(B) a corrected Quickstart description of ``scripts/install.sh``, which since
PUB-01 also installs ``requirements-dashboard.txt`` on every run.

These tests deliberately assert only substrings and the presence/position of
the new H3 - never README's total contents, byte length, line count, a
whole-file hash, or the complete list of H3 headings - because README.md is
edited by many stories over time.
"""

from __future__ import annotations

import re
from pathlib import Path

README_PATH = Path(__file__).resolve().parents[2] / "README.md"

# The 11 H2 sections README must keep, in this order. Mirrors the pre-existing
# tests/unit/test_readme_reference_split.py assertion so a regression is also
# caught in THIS file, not only there.
EXPECTED_H2_SECTIONS = [
    "Platform support",
    "Quickstart",
    "Components at a glance",
    "Architecture",
    "Personas (`~/.claude/agents/`)",
    "The overlord and the decision policy",
    "Reference",
    "Prerequisites",
    "Scheduler",
    "Reliability & limitations",
    "License",
]

GENERATOR_SCRIPT = "scripts/generate_launchd_plists.sh"
GENERATOR_FLAGS = ("--repo-root", "--out-dir", "--mlx-model-path")

# The exact stale Quickstart wording this story replaces: it told readers that
# install.sh installs requirements.txt alone, with no mention of the dashboard
# dependencies that PUB-01 made part of every install.
STALE_INSTALL_PHRASE = "installs `requirements.txt`, and reports"

_MISSING_H3_MSG = (
    "README's `## Scheduler` section has no `### ` heading about rendering/"
    "generating the launchd files. Add one there (suggested title: "
    "`### Rendering the launchd files for your machine`)."
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_ANCHOR_LINK_RE = re.compile(r"\]\(#([^)\s]+)\)")


# --- helpers ----------------------------------------------------------------


def _readme_text() -> str:
    assert README_PATH.exists(), f"README.md not found at {README_PATH}"
    return README_PATH.read_text(encoding="utf-8")


def _strip_fenced_code(text: str) -> list[str]:
    """README lines with fenced (```...```) code-block content blanked out, so
    ``#`` lines inside code blocks are not mistaken for headings."""
    lines: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            lines.append("")
        elif in_fence:
            lines.append("")
        else:
            lines.append(line)
    return lines


def _headings(stripped: list[str]) -> list[tuple[int, int, str]]:
    """(line_index, level, text) for every ATX heading outside code fences."""
    found: list[tuple[int, int, str]] = []
    for i, line in enumerate(stripped):
        match = _HEADING_RE.match(line)
        if match:
            found.append((i, len(match.group(1)), match.group(2)))
    return found


def _h2_texts(stripped: list[str]) -> list[str]:
    return [text for _, level, text in _headings(stripped) if level == 2]


def _section_bounds(stripped: list[str], h2_text: str) -> tuple[int, int]:
    """Line span [start, end) of the named H2 section (end exclusive)."""
    h2s = [(i, text) for i, level, text in _headings(stripped) if level == 2]
    starts = [i for i, text in h2s if text == h2_text]
    assert starts, f"README.md has no `## {h2_text}` heading"
    start = starts[0]
    later_h2s = [i for i, _ in h2s if i > start]
    end = later_h2s[0] if later_h2s else len(stripped)
    return start, end


def _launchd_generation_h3s(stripped: list[str]) -> list[tuple[int, str]]:
    """H3 headings whose text mentions rendering/generating the launchd
    files (e.g. the suggested `### Rendering the launchd files for your
    machine`)."""
    matches: list[tuple[int, str]] = []
    for i, level, text in _headings(stripped):
        if level != 3:
            continue
        lowered = text.lower()
        if "launchd" in lowered and ("generat" in lowered or "render" in lowered):
            matches.append((i, text))
    return matches


def _scheduler_launchd_sections() -> list[str]:
    """Raw text of every launchd-generation H3 subsection that sits inside the
    `## Scheduler` section."""
    text = _readme_text()
    stripped = _strip_fenced_code(text)
    raw_lines = text.splitlines()
    start, end = _section_bounds(stripped, "Scheduler")
    sections: list[str] = []
    for i, _heading in _launchd_generation_h3s(stripped):
        if start < i < end:
            sections.append("\n".join(raw_lines[i:end]))
    return sections


def _launchd_section_haystack() -> str:
    sections = _scheduler_launchd_sections()
    assert sections, _MISSING_H3_MSG
    return "\n".join(sections)


def _fenced_blocks(raw_lines: list[str]) -> list[str]:
    """Contents of the ```-fenced code blocks among the given raw lines."""
    blocks: list[str] = []
    current: list[str] = []
    in_fence = False
    for line in raw_lines:
        if line.lstrip().startswith("```"):
            if in_fence:
                blocks.append("\n".join(current))
                current = []
            in_fence = not in_fence
            continue
        if in_fence:
            current.append(line)
    if in_fence and current:
        blocks.append("\n".join(current))
    return blocks


def _github_anchor(heading_text: str) -> str:
    """GitHub-style anchor slug for an ATX heading's text."""
    text = heading_text.strip().replace("`", "").lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


# --- H2 structure guards (criteria 5 and 8) ---------------------------------


def test_readme_still_has_exactly_the_expected_h2_sections():
    stripped = _strip_fenced_code(_readme_text())
    h2_texts = _h2_texts(stripped)
    assert h2_texts == EXPECTED_H2_SECTIONS, (
        "README's `## ` headings changed - this story must add its content as "
        "an H3 inside an existing H2 section. Expected "
        f"{EXPECTED_H2_SECTIONS}, got {h2_texts}"
    )
    assert len(h2_texts) == 11


def test_no_new_h2_heading_was_introduced():
    stripped = _strip_fenced_code(_readme_text())
    assert len(_h2_texts(stripped)) == 11, (
        "README must keep exactly 11 `## ` sections; add new content as an H3 "
        "inside an existing H2 section instead"
    )


# --- (A) the new H3 under `## Scheduler` (criteria 1-4) ---------------------


def test_scheduler_section_has_an_h3_about_launchd_generation():
    matches = _launchd_generation_h3s(_strip_fenced_code(_readme_text()))
    assert matches, _MISSING_H3_MSG


def test_launchd_generation_h3_sits_inside_the_scheduler_section():
    text = _readme_text()
    stripped = _strip_fenced_code(text)
    matches = _launchd_generation_h3s(stripped)
    assert matches, _MISSING_H3_MSG
    start, end = _section_bounds(stripped, "Scheduler")
    inside = [i for i, _ in matches if start < i < end]
    assert inside, (
        "the launchd-generation H3(s) "
        f"{[heading for _, heading in matches]!r} must be added INSIDE the "
        "`## Scheduler` section (between the `## Scheduler` line and the next "
        f"`## ` heading, README lines {start + 1}-{end}), but they sit at "
        f"README lines {[i + 1 for i, _ in matches]}"
    )


def test_new_h3_names_the_generator_script():
    haystack = _launchd_section_haystack()
    assert GENERATOR_SCRIPT in haystack, (
        "the new H3 must tell readers to regenerate the launchd files with "
        f"`{GENERATOR_SCRIPT}`"
    )
    assert GENERATOR_SCRIPT in _readme_text()


def test_new_h3_documents_the_three_generator_flags():
    haystack = _launchd_section_haystack()
    for flag in GENERATOR_FLAGS:
        assert flag in haystack, (
            f"the new H3 must document the `{flag}` flag of "
            f"{GENERATOR_SCRIPT}"
        )


def test_new_h3_documents_the_mlx_model_path_env_var_alternative():
    haystack = _launchd_section_haystack()
    assert "MLX_MODEL_PATH" in haystack, (
        "the new H3 must document that MLX_MODEL_PATH can be set instead of "
        "passing --mlx-model-path"
    )


def test_new_h3_says_the_script_fails_closed_without_a_model_path():
    lowered = _launchd_section_haystack().lower()
    assert "fail" in lowered, (
        "the new H3 must say the script fails closed (exits with an error) "
        "when neither --mlx-model-path nor MLX_MODEL_PATH is supplied"
    )


def test_new_h3_explains_the_committed_files_are_a_reference_copy():
    lowered = _launchd_section_haystack().lower()
    assert "plist" in lowered, (
        "the new H3 must mention the committed launchd/*.plist files"
    )
    assert "newsyslog" in lowered, (
        "the new H3 must mention launchd/pipeline-logs.newsyslog.conf, which "
        "the same script renders"
    )
    assert "absolute" in lowered, (
        "the new H3 must explain that the committed files carry the "
        "maintainer's own absolute paths"
    )
    assert "reference" in lowered, (
        "the new H3 must explain that the committed files are checked in as "
        "a reference copy"
    )


def test_new_h3_shows_a_copy_pasteable_example_invocation():
    sections = _scheduler_launchd_sections()
    assert sections, _MISSING_H3_MSG
    blocks: list[str] = []
    for section in sections:
        blocks.extend(_fenced_blocks(section.splitlines()))
    examples = [block for block in blocks if GENERATOR_SCRIPT in block]
    assert examples, (
        "the new H3 must show a fenced, copy-pasteable example invocation of "
        f"{GENERATOR_SCRIPT}"
    )
    assert any(
        "--mlx-model-path" in block or "MLX_MODEL_PATH" in block
        for block in examples
    ), (
        "the example invocation must be concrete enough to actually run: it "
        "has to supply a model path via --mlx-model-path or MLX_MODEL_PATH, "
        "otherwise the script fails closed"
    )


def test_new_h3_cross_references_the_platform_support_note():
    haystack = _launchd_section_haystack()
    lowered = haystack.lower()
    assert "platform support" in lowered or "](#platform-support)" in haystack, (
        "the new H3 must cross-reference the `## Platform support` note (the "
        "launchd files are macOS-only; Linux users run the entry points under "
        "their own init system)"
    )


# --- (B) the corrected Quickstart install.sh description (criteria 6-7) -----


def test_quickstart_install_sh_description_includes_dashboard_requirements():
    text = _readme_text()
    stripped = _strip_fenced_code(text)
    start, end = _section_bounds(stripped, "Quickstart")
    quickstart = "\n".join(text.splitlines()[start:end])
    assert "requirements-dashboard.txt" in quickstart, (
        "the Quickstart description of scripts/install.sh must say it also "
        "installs requirements-dashboard.txt (PUB-01 made install.sh install "
        "the dashboard deps on every run)"
    )


def test_stale_install_sh_sentence_is_gone():
    normalized = re.sub(r"\s+", " ", _readme_text())
    assert STALE_INSTALL_PHRASE not in normalized, (
        "README still says install.sh 'installs `requirements.txt`, and "
        "reports' - update the Quickstart sentence: install.sh now also "
        "installs requirements-dashboard.txt"
    )


# --- anchor-link validity (criterion 9) -------------------------------------


def test_every_internal_anchor_link_in_readme_resolves():
    text = _readme_text()
    stripped = _strip_fenced_code(text)
    anchors = {
        _github_anchor(heading) for _, _, heading in _headings(stripped)
    }
    links = _ANCHOR_LINK_RE.findall("\n".join(stripped))
    assert links, "expected README to contain internal anchor links"
    broken = sorted({target for target in links if target not in anchors})
    assert not broken, (
        f"internal anchor links with no matching README heading: {broken}"
    )