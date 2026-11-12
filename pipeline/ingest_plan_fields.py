"""Plan-level field validators for ``ingest_plan``.

Pure checks on the plan dict, kept out of ``pipeline/ingest.py`` so that
module stays within the 1000-line limit.
"""

import re

from .test_scope import TEST_SCOPE_VALUES

_TEAM_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def _team_error(plan: dict) -> str | None:
    """Return an error string when the plan's optional ``team`` is malformed."""
    team = plan.get("team")
    if team is None:
        return None
    if not isinstance(team, str) or not _TEAM_RE.match(team):
        return (
            "plan field 'team' must be a lowercase slug matching "
            "^[a-z0-9][a-z0-9-]{0,31}$"
        )
    return None


def _test_scope_error(obj: dict, label: str = "plan") -> str | None:
    value = obj.get("test_scope")
    if value is None or (isinstance(value, str) and value in TEST_SCOPE_VALUES):
        return None
    return f"{label} field 'test_scope' must be one of 'full', 'scoped', got {value!r}"
