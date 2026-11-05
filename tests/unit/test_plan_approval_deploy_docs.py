"""Pin the plan-approval facts written into the standalone deploy docs.

These are membership checks against the files an operator actually reads: the
standalone compose file, the deploy guide and the changelog. They exist so a
later edit cannot quietly drop the dashboard public URL, the approval link
variable, the extension mount or the review endpoints from the documentation.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy" / "standalone" / "compose.yaml"
DEPLOY_DOC = ROOT / "docs" / "DEPLOY_STANDALONE.md"
CHANGELOG = ROOT / "CHANGELOG.md"


def test_compose_sets_dashboard_public_url() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert (
        "PIPELINE_DASHBOARD_PUBLIC_URL: https://${PUBLIC_HOSTNAME}:${PUBLIC_PORT:-8443}"
        in text
    )


def test_deploy_doc_documents_approval_and_extension_mount() -> None:
    text = DEPLOY_DOC.read_text(encoding="utf-8")
    for needle in (
        "### Approving plans",
        "plan_pending_approval",
        "PIPELINE_DASHBOARD_PUBLIC_URL",
        "PIPELINE_APPROVAL_LINK_PATH",
        "PIPELINE_DASHBOARD_PLUGINS",
        "/review/approve",
    ):
        assert needle in text
    assert text.index("### Approving plans") < text.index("## 7. Upgrading")


def test_deploy_doc_promises_no_browser_review_page() -> None:
    text = DEPLOY_DOC.read_text(encoding="utf-8")
    assert "/review.html" not in text


def test_changelog_unreleased_mentions_plan_approval() -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    start = text.index("## [Unreleased]")
    end = text.find("## [", start + 1)
    section = text[start:end if end != -1 else len(text)]
    assert "plan_pending_approval" in section
    assert "PIPELINE_DASHBOARD_PLUGINS" in section
