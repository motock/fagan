"""Tests for making the interactive provider picker discoverable.

The story adds *documentation pointers* to the already-shipped
``scripts/choose_providers.py`` picker at the two moments an operator needs
it:

(a) ``scripts/install.sh``'s closing "Done. Next steps:" block names the
    picker as the interactive way to choose a provider per role -- without
    ever *running* it (the installer must stay non-interactive and must never
    block).
(b) ``README.md``'s existing "Provider selection & authorization" section
    describes the picker as the interactive third way to select a provider,
    noting that it configures each of the nine roles independently and is
    safe to re-run.

These tests are RED until those two documentation edits land. They assert
only what this story adds; the README's H2 heading list is a shared artifact
that later stories may legitimately extend, so the boundary check here pins
the *current* count/order and additionally requires agreement with
``tests/unit/test_readme_reference_split.py`` (the canonical pin) rather than
inventing a second, divergent expectation.
"""

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
README = REPO_ROOT / "README.md"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
PICKER = REPO_ROOT / "scripts" / "choose_providers.py"
SIBLING_TEST = REPO_ROOT / "tests" / "unit" / "test_readme_reference_split.py"

# The section that must gain the picker pointer. It is an H3 inside README.md
# (NOT an H2 -- adding an H2 would break the pinned heading list).
PROVIDER_SECTION_TITLE = "Provider selection & authorization"

# Mirrors EXPECTED_README_SECTIONS in tests/unit/test_readme_reference_split.py.
# test_readme_h2_list_agrees_with_reference_split_test asserts the two lists
# stay identical, so this copy cannot silently drift from the canonical pin.
EXPECTED_README_SECTIONS = [
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


# ---------------------------------------------------------------------------
# Helpers (same extraction approach as test_readme_reference_split.py)
# ---------------------------------------------------------------------------

def h2_headings(text: str) -> list[str]:
    """Return the ordered list of H2 (##) heading titles in `text`.

    Headings are matched on lines that start at column 0 with '## '.
    """
    headings: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            headings.append(line[3:].strip())
    return headings


def _h3_section_body(text: str, title: str) -> str:
    """Return the body of an H3 section (heading line + body up to the next
    H2 or H3 heading), with surrounding whitespace stripped.

    Locating the section by its heading and slicing only that region keeps the
    picker assertion from being satisfied by an unrelated mention elsewhere in
    the file.
    """
    lines = text.splitlines()
    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        if start is None and line.startswith(f"### {title}"):
            start = i
            continue
        if start is not None and line.startswith(("## ", "### ")):
            end = i
            break
    assert start is not None, (
        f"README.md must contain the H3 '### {title}' section"
    )
    return "\n".join(lines[start:end]).strip()


def _load_sibling_module():
    """Import tests/unit/test_readme_reference_split.py by path.

    tests/unit is not a package, so a plain import would not resolve; loading
    by file location lets this test reuse the canonical heading pin instead of
    duplicating it.
    """
    assert SIBLING_TEST.is_file(), f"{SIBLING_TEST} must exist"
    spec = importlib.util.spec_from_file_location(
        "_readme_reference_split_for_picker_docs", SIBLING_TEST
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Preconditions: the artifacts this story edits exist
# ---------------------------------------------------------------------------

def test_install_sh_and_readme_exist():
    assert INSTALL_SH.is_file(), "scripts/install.sh must exist"
    assert README.is_file(), "README.md must exist at the repo root"


def test_picker_script_exists():
    # The docs point at a real script; if the picker were renamed/removed the
    # pointers would be dead links.
    assert PICKER.is_file(), "scripts/choose_providers.py must exist"


# ---------------------------------------------------------------------------
# (a) scripts/install.sh names the picker in its closing next-steps block
# ---------------------------------------------------------------------------

def test_install_sh_mentions_choose_providers():
    text = INSTALL_SH.read_text()
    assert "choose_providers.py" in text, (
        "scripts/install.sh must name scripts/choose_providers.py as the "
        "interactive way to choose a provider per role"
    )


def test_install_sh_mention_is_in_the_closing_next_steps_block():
    """The pointer must live in the '==> Done. Next steps:' block, not
    somewhere arbitrary in the installer."""
    lines = INSTALL_SH.read_text().splitlines()
    marker = None
    for i, line in enumerate(lines):
        if "Done. Next steps:" in line:
            marker = i
            break
    assert marker is not None, (
        "scripts/install.sh must keep its '==> Done. Next steps:' block"
    )
    tail = "\n".join(lines[marker:])
    assert "choose_providers.py" in tail, (
        "the choose_providers.py pointer must appear in the closing "
        "'Done. Next steps:' block of scripts/install.sh"
    )


def test_install_sh_does_not_run_the_picker():
    """Guard against a future edit making the installer interactive.

    Any line that mentions the picker must be inert: a shell comment or an
    `echo` of instructions. A line that would actually execute it (e.g.
    `.venv/bin/python scripts/choose_providers.py`) fails this test.
    """
    offenders = []
    lines = INSTALL_SH.read_text().splitlines()
    for lineno, line in enumerate(lines, start=1):
        if "choose_providers.py" not in line:
            continue
        stripped = line.strip()
        if stripped.startswith(("#", "echo")):
            continue
        # A backslash-continued line is part of the preceding echo/comment.
        prev = lines[lineno - 2].rstrip() if lineno >= 2 else ""
        if prev.endswith("\\"):
            continue
        offenders.append(f"{lineno}: {line}")
    assert not offenders, (
        "scripts/install.sh must NOT invoke the picker (it must stay "
        "non-interactive and never block); offending lines: "
        f"{offenders}"
    )


def test_install_sh_has_no_execution_of_the_picker():
    """Independent, regex-based check for an execution of the picker."""
    pattern = re.compile(
        r"(^|[;&|]\s*|\$\()\s*"
        r"(\.venv/bin/python3?|python3?|bash|sh|source|\.)\b[^\n]*"
        r"choose_providers\.py"
    )
    text = INSTALL_SH.read_text()
    match = pattern.search(text)
    assert match is None, (
        "scripts/install.sh must not execute scripts/choose_providers.py; "
        f"found: {match.group(0)!r}"
    )


def test_install_sh_is_non_interactive():
    """The installer must never block on a prompt."""
    prompts = [
        f"{lineno}: {line}"
        for lineno, line in enumerate(INSTALL_SH.read_text().splitlines(), 1)
        if re.search(r"(^|[;&|]\s*)read\s+(-[a-zA-Z]+\s+)*\w", line)
    ]
    assert not prompts, (
        "scripts/install.sh must stay non-interactive (no `read` prompts); "
        f"found: {prompts}"
    )


def test_install_sh_is_syntactically_valid():
    """`bash -n scripts/install.sh` must exit 0."""
    result = subprocess.run(
        ["bash", "-n", str(INSTALL_SH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "bash -n scripts/install.sh must exit 0; "
        f"stderr: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# (b) README.md's 'Provider selection & authorization' section names it
# ---------------------------------------------------------------------------

def test_readme_provider_section_mentions_choose_providers():
    body = _h3_section_body(README.read_text(), PROVIDER_SECTION_TITLE)
    assert "choose_providers.py" in body, (
        "README.md's 'Provider selection & authorization' section must name "
        "scripts/choose_providers.py as the interactive way to pick a provider"
    )


def test_readme_provider_section_calls_it_interactive():
    body = _h3_section_body(README.read_text(), PROVIDER_SECTION_TITLE)
    assert re.search(r"interactive", body, re.IGNORECASE), (
        "the picker must be described as the interactive way to select a "
        "provider in README.md's 'Provider selection & authorization' section"
    )


def test_readme_provider_section_mentions_nine_roles():
    body = _h3_section_body(README.read_text(), PROVIDER_SECTION_TITLE)
    assert re.search(r"\b(nine|9)\b", body, re.IGNORECASE), (
        "the section must state that the picker configures each of the nine "
        "roles independently"
    )


def test_readme_provider_section_says_safe_to_rerun():
    body = _h3_section_body(README.read_text(), PROVIDER_SECTION_TITLE)
    assert re.search(r"re-?run", body, re.IGNORECASE), (
        "the section must state that the picker is safe to re-run"
    )


def test_readme_provider_section_is_still_an_h3():
    """The section must not be promoted to an H2 (that would break the pinned
    heading list)."""
    headings = h2_headings(README.read_text())
    assert PROVIDER_SECTION_TITLE not in headings, (
        "'Provider selection & authorization' must remain an H3, not an H2"
    )
    text = README.read_text()
    assert f"### {PROVIDER_SECTION_TITLE}" in text, (
        f"README.md must keep its '### {PROVIDER_SECTION_TITLE}' heading"
    )


# ---------------------------------------------------------------------------
# (4) NEGATIVE/BOUNDARY: README H2 headings unchanged in count and order
# ---------------------------------------------------------------------------

def test_readme_h2_count_and_order_unchanged():
    headings = h2_headings(README.read_text())
    assert headings == EXPECTED_README_SECTIONS, (
        "README.md's H2 sections must be unchanged by this story; "
        f"expected {EXPECTED_README_SECTIONS}; got {headings}"
    )


def test_readme_h2_count_is_eleven():
    headings = h2_headings(README.read_text())
    assert len(headings) == 11, (
        f"README.md must still have exactly 11 H2 sections, got "
        f"{len(headings)}: {headings}"
    )


def test_readme_h2_list_agrees_with_reference_split_test():
    """The two test modules must agree on the README heading pin, so a future
    edit cannot satisfy one and break the other."""
    sibling = _load_sibling_module()
    assert sibling.EXPECTED_README_SECTIONS == EXPECTED_README_SECTIONS, (
        "tests/unit/test_provider_picker_docs.py and "
        "tests/unit/test_readme_reference_split.py must pin the same README "
        "H2 sections"
    )
    assert sibling.h2_headings(README.read_text()) == h2_headings(
        README.read_text()
    ), "the two modules must extract README H2 headings identically"


def test_readme_has_no_new_h2_section_for_the_picker():
    """Boundary: no H2 heading may mention the picker."""
    headings = h2_headings(README.read_text())
    offenders = [h for h in headings if "picker" in h.lower()]
    assert not offenders, (
        "the picker must be documented inside the existing 'Provider "
        f"selection & authorization' section, not a new H2: {offenders}"
    )


@pytest.mark.parametrize("title", EXPECTED_README_SECTIONS)
def test_readme_expected_sections_present(title):
    """Membership check (not just exact-match) so a failure names the missing
    section."""
    assert title in h2_headings(README.read_text()), (
        f"README.md must still contain the H2 '## {title}'"
    )
