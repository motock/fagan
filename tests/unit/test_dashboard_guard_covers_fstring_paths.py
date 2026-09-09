"""Regression guard: the PLAN_DIR decoupling guard must cover f-string paths.

CFG-A3 reintroduced on-disk-layout parsing in app/dashboard.py via an
f-string path composition -- ``PLAN_DIR / f"{plan_name}.notifications.jsonl"``
-- which slipped past the existing decoupling guard in
tests/unit/test_dashboard_no_direct_plan_dir.py because that guard's
_FORBIDDEN_PATTERNS list only covered ``PLAN_DIR.glob(...)``.

This test grades the WIDENING of that guard: the pattern list must gain an
entry that matches f-string path composition off PLAN_DIR, so a future
``PLAN_DIR / f"..."`` cannot be reintroduced silently. It does NOT re-introduce
the pattern into app/dashboard.py -- the synthetic offending source is built
here, in this test, and matched against the guard's own regex list.

Note: _FORBIDDEN_PATTERNS is a CUMULATIVE list that later stories may extend,
so every assertion here is membership/behavior based -- never an exact-match
on the list's full contents, length, or order.
"""
import inspect
import re

from app import dashboard
from tests.unit.test_dashboard_no_direct_plan_dir import _FORBIDDEN_PATTERNS


def _source() -> str:
    """Return the full source text of app/dashboard.py as a single string."""
    return inspect.getsource(dashboard)


# The synthetic offending line, built HERE (never written into
# app/dashboard.py). It mirrors the CFG-A3 defect: f-string path composition
# off PLAN_DIR.
_OFFENDING_FSTRING_SNIPPET = 'PLAN_DIR / f"{plan_name}.notifications.jsonl"'

# The one intentional survivor: a plain (non-f-string) string operand. The
# widened guard must NOT match it, or the guard would break the sanctioned
# exception in _dashboard_ui_state_path.
_SANCTIONED_PLAIN_STRING_SNIPPET = 'PLAN_DIR / ".dashboard_ui_state.json"'


def test_guard_patterns_fire_on_fstring_path_composition():
    """At least one guard pattern must match the synthetic CFG-A3 f-string.

    Runs the guard's own regex list (imported from
    test_dashboard_no_direct_plan_dir) against a synthetic source string
    containing ``PLAN_DIR / f"..."`` and asserts the widened guard actually
    fires on it.
    """
    assert _FORBIDDEN_PATTERNS, (
        "_FORBIDDEN_PATTERNS is empty; the guard cannot fire on anything"
    )
    matches = [
        pat for pat in _FORBIDDEN_PATTERNS if re.search(pat, _OFFENDING_FSTRING_SNIPPET)
    ]
    assert matches, (
        "no _FORBIDDEN_PATTERNS entry matches the CFG-A3 f-string path "
        f"composition {_OFFENDING_FSTRING_SNIPPET!r}; the guard has a hole "
        "and PLAN_DIR / f\"...\" can be reintroduced silently"
    )


def test_guard_patterns_do_not_fire_on_sanctioned_plain_string_path():
    """NEGATIVE: the guard must not match the sanctioned plain-string survivor.

    ``PLAN_DIR / ".dashboard_ui_state.json"`` is the intentional exception
    (a plain string operand, not an f-string). The widened pattern must be
    narrow enough to leave it alone, or the guard would break
    _dashboard_ui_state_path.
    """
    offenders = [
        pat
        for pat in _FORBIDDEN_PATTERNS
        if re.search(pat, _SANCTIONED_PLAIN_STRING_SNIPPET)
    ]
    assert offenders == [], (
        "_FORBIDDEN_PATTERNS now matches the sanctioned plain-string survivor "
        f"{_SANCTIONED_PLAIN_STRING_SNIPPET!r} (matched by: {offenders!r}); "
        "the widened pattern must cover f-string composition only and leave "
        "the intentional exception alone"
    )


def test_fstring_path_pattern_is_member_of_forbidden_patterns():
    """MEMBERSHIP: a PLAN_DIR / f-string pattern is in _FORBIDDEN_PATTERNS.

    Membership only -- _FORBIDDEN_PATTERNS is cumulative, so this must never
    assert the list's exact contents, length, or order. We assert that at
    least one entry both fires on the CFG-A3 f-string composition and spares
    the sanctioned plain-string survivor; the pre-existing glob-only entry
    cannot match the f-string snippet, so any such entry is the new one.
    """
    covering_entries = [
        pat
        for pat in _FORBIDDEN_PATTERNS
        if re.search(pat, _OFFENDING_FSTRING_SNIPPET)
        and not re.search(pat, _SANCTIONED_PLAIN_STRING_SNIPPET)
    ]
    assert covering_entries, (
        "_FORBIDDEN_PATTERNS has no entry covering f-string path composition "
        "off PLAN_DIR; add one (comment it as the CFG-A3 regression guard)"
    )


def test_dashboard_source_has_no_fstring_plan_dir_composition():
    """app/dashboard.py itself must not contain the CFG-A3 pattern.

    The widening exists to keep the defect out; this asserts the production
    source is currently clean of ``PLAN_DIR / f"..."`` so the guard and the
    source agree. (The synthetic offending string lives only in this test.)
    """
    src = _source()
    offenders = [
        pat
        for pat in _FORBIDDEN_PATTERNS
        if re.search(pat, src)
    ]
    assert offenders == [], (
        "app/dashboard.py contains forbidden PLAN_DIR parse pattern(s): "
        f"{offenders!r}"
    )


def test_dashboard_source_still_has_sanctioned_plain_string_survivor():
    """The sanctioned survivor must remain in app/dashboard.py.

    Guards the negative case's premise: the plain-string
    ``PLAN_DIR / ".dashboard_ui_state.json"`` composition is still present in
    the production source, so the guard's narrowness is load-bearing (it is
    what keeps this intentional exception passing the widened guard).
    """
    src = _source()
    assert _SANCTIONED_PLAIN_STRING_SNIPPET in src, (
        "the sanctioned survivor "
        f"{_SANCTIONED_PLAIN_STRING_SNIPPET!r} is missing from "
        "app/dashboard.py; the guard's negative case is no longer load-bearing"
    )