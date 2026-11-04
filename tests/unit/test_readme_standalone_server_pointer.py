"""RDV-04: README must point at the Docker standalone server deployment.

README.md has two different "standalone" stories. The section
``### Running standalone (dashboard + scheduler, no MCP server)`` documents the
``scripts/standalone-setup.sh`` flow, which runs no MCP server. The deployable
server stack -- Keycloak sign-in, Caddy TLS, remote MCP transport -- is
``docs/DEPLOY_STANDALONE.md``. The README must link to it, must say that plan
approval is on by default there, and must not bury that pointer after the
script flow it is meant to be distinguished from.

Per-story test file so sibling stories never collide on a shared assertion.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
README_PATH = REPO_ROOT / "README.md"
GUIDE_RELATIVE_PATH = "docs/DEPLOY_STANDALONE.md"

NEW_HEADING = "### Running the standalone server (Docker)"
SCRIPT_FLOW_HEADING = "### Running standalone (dashboard + scheduler, no MCP server)"


def _readme_text() -> str:
    return README_PATH.read_text(encoding="utf-8")


def test_readme_has_standalone_server_heading():
    assert NEW_HEADING in _readme_text()


def test_readme_links_to_the_standalone_deployment_guide():
    assert (REPO_ROOT / GUIDE_RELATIVE_PATH).is_file()
    assert GUIDE_RELATIVE_PATH in _readme_text()


def test_readme_mentions_plan_approval_variable():
    assert "PIPELINE_PLAN_APPROVAL" in _readme_text()


def test_server_heading_precedes_the_script_flow_heading():
    text = _readme_text()
    server_index = text.find(NEW_HEADING)
    script_index = text.find(SCRIPT_FLOW_HEADING)
    # A missing heading makes .find() return -1, and -1 < anything is True;
    # assert presence first so a deleted heading cannot pass this ordering test.
    assert server_index != -1, f"README.md is missing {NEW_HEADING!r}"
    assert script_index != -1, f"README.md is missing {SCRIPT_FLOW_HEADING!r}"
    assert server_index < script_index


def test_readme_preamble_still_has_no_section_heading():
    preamble_lines = []
    for line in _readme_text().splitlines():
        if line.startswith("## "):
            break
        preamble_lines.append(line)
    preamble = "\n".join(preamble_lines)
    assert "## " not in preamble
