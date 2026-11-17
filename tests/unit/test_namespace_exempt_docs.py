"""Doc contracts for the namespace-exempt extension paths setting.

These tests pin the deployment reference and changelog prose to the name of the
setting, so a rename in the settings model cannot silently leave the docs stale.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DOC = REPO_ROOT / "docs" / "DEPLOY_STANDALONE.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

TARGET_ROW_PREFIX = "| `PIPELINE_TENANT_DATA_EXEMPT_PATHS` |"
DASHBOARD_ROW_PREFIX = "| `PIPELINE_DASHBOARD_PLUGINS` |"


def _deploy_lines():
    return DEPLOY_DOC.read_text(encoding="utf-8").splitlines()


def test_target_row_exists():
    assert any(line.startswith(TARGET_ROW_PREFIX) for line in _deploy_lines()), (
        "DEPLOY_STANDALONE.md has no PIPELINE_TENANT_DATA_EXEMPT_PATHS table row"
    )


def test_target_row_describes_exempt_paths():
    rows = [line for line in _deploy_lines() if line.startswith(TARGET_ROW_PREFIX)]
    assert len(rows) == 1, f"expected exactly one target row, found {len(rows)}"
    row = rows[0]
    assert "/api/ext/" in row, "target row must name the /api/ext/ path prefix"
    assert "must still authenticate" in row, (
        "target row must state that callers must still authenticate"
    )


def test_target_row_follows_dashboard_row_in_configuration_reference():
    lines = _deploy_lines()
    dashboard_rows = [
        index
        for index, line in enumerate(lines)
        if line.startswith(DASHBOARD_ROW_PREFIX)
    ]
    target_rows = [
        index for index, line in enumerate(lines) if line.startswith(TARGET_ROW_PREFIX)
    ]
    assert len(dashboard_rows) == 1, (
        f"expected exactly one PIPELINE_DASHBOARD_PLUGINS row, found "
        f"{len(dashboard_rows)}"
    )
    assert len(target_rows) == 1, (
        f"expected exactly one target row, found {len(target_rows)}"
    )
    dashboard_index = dashboard_rows[0]
    target_index = target_rows[0]
    next_section = next(
        (
            index
            for index, line in enumerate(lines)
            if index > dashboard_index and line.startswith("## ")
        ),
        len(lines),
    )
    assert dashboard_index < target_index < next_section, (
        f"target row at line {target_index + 1} must sit after the dashboard row "
        f"(line {dashboard_index + 1}) and before the next section heading "
        f"(line {next_section + 1})"
    )


def test_changelog_mentions_setting():
    assert "PIPELINE_TENANT_DATA_EXEMPT_PATHS" in CHANGELOG.read_text(
        encoding="utf-8"
    ), "CHANGELOG.md does not mention PIPELINE_TENANT_DATA_EXEMPT_PATHS"
