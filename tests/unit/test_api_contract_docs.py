"""Pin the dashboard API-contract documentation to the shipped behaviour.

``docs/API_CONTRACT.md`` is the operator-facing description of the generated
OpenAPI contract and the two ways to host a replacement dashboard UI. These
assertions keep it honest: the names it advertises must be the ones the code
reads, the commands it prints must be the ones that exist, and the insecure
CORS shorthand must never creep back in.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

API_CONTRACT_DOC = ROOT / "docs" / "API_CONTRACT.md"
REFERENCE = ROOT / "REFERENCE.md"
CHANGELOG = ROOT / "CHANGELOG.md"

REQUIRED_DOC_STRINGS = (
    "/api/openapi.json",
    "API_VERSION",
    "PIPELINE_DASHBOARD_UI",
    "PIPELINE_DASHBOARD_CORS_ORIGINS",
    "X-Pipeline-API-Key",
    "python scripts/api_contract.py export",
)

REQUIRED_REFERENCE_VARIABLES = (
    "PIPELINE_DASHBOARD_UI",
    "PIPELINE_DASHBOARD_PUBLIC_DOCS",
    "PIPELINE_DASHBOARD_CORS_ORIGINS",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _unreleased_section() -> str:
    text = _read(CHANGELOG)
    start = text.index("## [Unreleased]")
    next_release = text.find("\n## [", start + len("## [Unreleased]"))
    if next_release == -1:
        return text[start:]
    return text[start:next_release]


def test_api_contract_doc_states_the_contract_surface() -> None:
    text = _read(API_CONTRACT_DOC)
    for expected in REQUIRED_DOC_STRINGS:
        assert expected in text, f"docs/API_CONTRACT.md must mention {expected!r}"


def test_reference_lists_the_dashboard_ui_variables() -> None:
    text = _read(REFERENCE)
    for name in REQUIRED_REFERENCE_VARIABLES:
        assert name in text, f"REFERENCE.md must document {name}"


def test_changelog_unreleased_covers_the_public_docs_change() -> None:
    section = _unreleased_section()
    assert "PIPELINE_DASHBOARD_PUBLIC_DOCS" in section
    assert "/api/openapi.json" in section


def test_api_contract_doc_never_documents_a_cors_wildcard() -> None:
    text = _read(API_CONTRACT_DOC)
    assert "allow_origins=['*']" not in text
    assert "PIPELINE_DASHBOARD_CORS_ORIGINS=*" not in text


def test_every_dashboard_variable_in_the_contract_doc_is_in_the_reference() -> None:
    doc_text = _read(API_CONTRACT_DOC)
    reference_text = _read(REFERENCE)
    documented = set(re.findall(r"PIPELINE_DASHBOARD_[A-Z_]+", doc_text))
    missing = sorted(name for name in documented if name not in reference_text)
    assert not missing, f"REFERENCE.md does not document {missing}"
