"""Documentation guards for operator-supplied plugin adapters (ADR-style seams).

These tests read the shipped docs as text: they pin that every plugin
environment variable is documented, that the Plugins section sits where an
operator will look for it, and that the sentences the plugin seams made false
are gone for good.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REFERENCE = _REPO_ROOT / "REFERENCE.md"
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"

PLUGIN_VARIABLES = (
    "PIPELINE_IDENTITY_PLUGIN",
    "PIPELINE_AUTHORIZER_PLUGIN",
    "PIPELINE_EVENT_BUS_PLUGIN",
    "PIPELINE_FORGE_PLUGIN",
    "PIPELINE_TICKET_PROVIDER_PLUGIN",
    "PIPELINE_CHANNEL_PLUGINS",
)

# Sentences the plugin seams falsified: each claimed a closed set of built-in
# adapters, or that only OidcProvider can face the network. Collapsed-whitespace
# form so a reflow does not silently re-admit them.
FALSIFIED_SENTENCES = (
    "Any other value raises at startup rather than silently downgrading authentication.",
    "A network transport refuses to start unless `PIPELINE_IDENTITY_PROVIDER=oidc`.",
    "the transport must be named *and* OIDC must be configured",
    (
        "`BearerAuthMiddleware` refuses to be constructed with anything that is not an "
        "`OidcProvider` — it raises `TypeError` — so the default `SharedSecretProvider` "
        "cannot be wired in by accident."
    ),
    "# Starts: OIDC is what makes the endpoint authenticating.",
    "Bus selection: `inprocess`, `jsonl` or `nats`.",
    "Every other value keeps the in-process bus.",
    "`auto` \\| `none` \\| `plane` \\| `jira` — see table above |",
    "the code host whose CLI the pipeline drives. `auto` resolves",
    "A channel type is a class in `pipeline/` registered with `register_channel_type`",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _collapsed(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture(scope="module")
def reference() -> str:
    return _text(_REFERENCE)


@pytest.fixture(scope="module")
def changelog() -> str:
    return _text(_CHANGELOG)


@pytest.mark.parametrize("variable", PLUGIN_VARIABLES)
def test_every_plugin_variable_is_documented(reference: str, variable: str) -> None:
    assert variable in reference, f"{variable} is not documented in REFERENCE.md"


def test_plugins_section_precedes_environment_configuration(reference: str) -> None:
    plugins = reference.find("## Plugins (operator-supplied adapters)")
    configuration = reference.find("## Configuration (environment variables)")
    assert plugins != -1, "missing '## Plugins (operator-supplied adapters)'"
    assert configuration != -1, "missing '## Configuration (environment variables)'"
    assert plugins < configuration


def test_network_grade_capability_is_documented(reference: str) -> None:
    assert "network_grade" in reference


def test_no_table_row_is_wrapped_across_lines(reference: str) -> None:
    """GFM ends a table row at the newline: a wrapped row loses its last cell.

    Guards the defect where the ``PIPELINE_IDENTITY_PROVIDER`` row was split
    over three physical lines, truncating the table and spilling the
    continuation into body text. A collapsed-text grep cannot see that, so this
    check is line-based: a row line that does not close its final cell must be
    followed by another row line.
    """
    lines = reference.splitlines()
    wrapped = [
        number
        for number, (line, following) in enumerate(
            zip(lines, lines[1:] + [""], strict=True), start=1
        )
        if line.startswith("|") and not line.endswith("|") and not following.startswith("|")
    ]
    assert not wrapped, f"REFERENCE.md table rows wrapped across lines: {wrapped}"


def test_identity_provider_row_carries_both_plugin_and_builtin_note(
    reference: str,
) -> None:
    rows = [
        line
        for line in reference.splitlines()
        if line.startswith("| `PIPELINE_IDENTITY_PROVIDER` |")
    ]
    assert len(rows) == 1, "expected exactly one PIPELINE_IDENTITY_PROVIDER row"
    row = rows[0]
    assert row.endswith("|"), "the row must close its final cell on the same line"
    assert "PIPELINE_IDENTITY_PLUGIN" in row


@pytest.mark.parametrize("sentence", FALSIFIED_SENTENCES)
def test_falsified_sentences_are_gone(reference: str, sentence: str) -> None:
    assert sentence not in _collapsed(reference), (
        f"REFERENCE.md still claims a closed built-in set: {sentence!r}"
    )


def _heading_section(text: str, heading: str) -> list[str]:
    """Return the lines after ``heading`` up to the next heading of its level."""
    lines = text.splitlines()
    start = lines.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    section: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.lstrip("#")
        indent = len(line) - len(stripped)
        if line.startswith("#") and indent <= level:
            break
        section.append(line)
    return section


def test_changelog_added_heading_sits_directly_under_unreleased(changelog: str) -> None:
    lines = [line for line in changelog.splitlines()]
    unreleased = lines.index("## [Unreleased]")
    following = [line for line in lines[unreleased + 1 :] if line.strip()]
    assert following, "## [Unreleased] section is empty"
    assert following[0] == "### Added", (
        f"first heading under ## [Unreleased] is {following[0]!r}, expected '### Added'"
    )
    added = lines.index("### Added")
    fixed = lines.index("### Fixed")
    assert added < fixed


def test_changelog_added_bullet_names_plugin_variables_and_network_grade(
    changelog: str,
) -> None:
    section = _heading_section(changelog, "### Added")
    bullets: list[str] = []
    current: str | None = None
    for line in section:
        if line.startswith("- "):
            if current is not None:
                bullets.append(current)
            current = line
        elif current is not None:
            current += " " + line.strip()
    if current is not None:
        bullets.append(current)
    assert any(
        "PIPELINE_" in bullet and "network_grade" in bullet for bullet in bullets
    ), "the ### Added bullet must mention PIPELINE_ and network_grade"
