"""Contract for ``deploy/standalone/.env.example``.

The standalone ``compose.yaml`` reads its required secrets through
``${VAR:?...}``, which fails closed when a value is unset *or empty*. The
example file therefore ships the required secrets blank: an operator who copies
the file verbatim gets a stack that refuses to start until they fill those
entries in, rather than one that boots with a published, guessable credential.

Read as text (no dotenv dependency) so a dropped line, a non-empty value or a
new secret-looking entry all fail on assertion rather than on import.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ENV_EXAMPLE = (
    Path(__file__).resolve().parents[2] / "deploy" / "standalone" / ".env.example"
)

# The three entries compose requires for the stack to boot at all.
REQUIRED_SECRET_NAMES = (
    "KEYCLOAK_ADMIN_PASSWORD",
    "PIPELINE_DASHBOARD_CLIENT_SECRET",
    "PIPELINE_MCP_CLIENT_SECRET",
)

# Any name that reads as a credential must not carry a literal value: a
# shipped example value is a public, guessable credential.
NON_EMPTY_SECRET_LINE = re.compile(
    r"^[A-Z0-9_]*(?:SECRET|PASSWORD|TOKEN)[A-Z0-9_]*=\S+$", re.MULTILINE
)


def _text() -> str:
    assert ENV_EXAMPLE.is_file(), f"{ENV_EXAMPLE} does not exist"
    return ENV_EXAMPLE.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", REQUIRED_SECRET_NAMES)
def test_required_secret_is_present_and_empty(name: str) -> None:
    """Each required secret line exists and assigns nothing."""
    pattern = re.compile(rf"^{re.escape(name)}=$", re.MULTILINE)
    assert pattern.search(_text()), (
        f"{name} must appear as a bare `{name}=` line: compose's `:?` guard "
        "fails closed on an empty value, which is what keeps a copied example "
        "from booting with a published credential"
    )


def test_no_secret_shaped_entry_has_a_value() -> None:
    """No SECRET/PASSWORD/TOKEN entry ships a literal value."""
    offenders = NON_EMPTY_SECRET_LINE.findall(_text())
    assert not offenders, (
        "these entries ship a non-empty value where the example must stay "
        f"blank: {offenders}"
    )
