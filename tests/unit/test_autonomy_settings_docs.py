"""Docs tests for the autonomy settings keys (docs-only task)."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE = REPO_ROOT / "REFERENCE.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"


def _section(text: str, heading: str) -> str:
    """Return text from `heading` up to the next top-level '## ' line."""
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text if end == -1 else text[start:end]


def _tenant_settings() -> str:
    return _section(REFERENCE.read_text(encoding="utf-8"), "## Tenant settings")


def _unreleased() -> str:
    return _section(CHANGELOG.read_text(encoding="utf-8"), "## [Unreleased]")


def test_reference_tenant_settings_lists_autonomy_keys():
    lines = _tenant_settings().splitlines()
    for key in ("autonomy", "auto_merge", "triage_mode"):
        assert any(line.startswith(f"| `{key}` |") for line in lines), (
            f"missing table row starting with '| `{key}` |' in ## Tenant settings"
        )


def test_reference_tenant_settings_documents_env_default():
    section = _tenant_settings()
    assert "PIPELINE_AUTONOMY" in section
    assert "stands in for the deployment layer" in section


def test_changelog_unreleased_mentions_triage_mode():
    assert "triage_mode" in _unreleased()
