"""Pure parsing helpers for the Guard cell of docs/failure_modes.json.

The Guard cell is free text written by humans.  Two helpers turn it into
candidate regression-test file references:

- ``parse_guard_paths(raw)`` -> list[str]: zero or more candidate test-file
  references (plain file NAMES or tests/-relative paths), stripped of
  backticks and trailing parenthetical annotations.
- ``is_no_guard_note(raw)`` -> bool: True when the cell asserts no guard
  exists ("none ..." forms).  Used by the checker story to distinguish
  "no guard expected" from "guard cited but missing".

Contract:

- Callers pass ``str`` only.  ``None`` is not a valid input and raises
  ``TypeError``; no *string* input ever raises, however mangled.
- Parsing stays dumb and literal: a ``.py`` token that is clearly not a
  test (e.g. ``local_agent.py`` -- no ``test_`` prefix, no ``tests/``
  prefix) STILL yields a candidate.  Existence filtering and
  test-vs-non-test judgement is the later checker story's job, not ours.
- Pure functions: no I/O, no subprocesses, no module-level mutable state.
"""

import re

# One cited unit is either a backticked token (group 1) or a bare path
# ending in .py (group 2).  A "(...)" note DIRECTLY after the token is
# consumed and dropped (rule e) -- consuming it here is what keeps a comma
# or a second backticked name INSIDE the note from leaking in as a phantom
# candidate, and what keeps an unbalanced note (truncated cells) from
# breaking the token itself.
_CITED_UNIT = re.compile(
    r"`([^`]+)`"  # group 1: backticked token
    r"(?:\s*\([^()]*\))?"  # optional "(...)" note directly after it
    r"|((?:[\w.-]+/)*[\w.-]+\.py)"  # group 2: bare path ending in .py
    r"(?![\w.-])"  # so test_x.py.bak does not yield test_x.py
    r"(?:\s*\([^()]*\))?"  # optional "(...)" note directly after it
)

# "none" as the whole FIRST word/token, case-insensitive.  The \b is what
# makes this a whole-word check ("nonexistent guard" and "nonetheless" are
# NOT no-guard notes) and re.match anchors it at the start (a citation that
# merely mentions "none" later in prose is a guard citation, not a no-guard
# note -- so no re.search, which would scan the whole cell).
_NO_GUARD_RE = re.compile(r"none\b", re.IGNORECASE)


def is_no_guard_note(raw: str) -> bool:
    """Return True when the cell asserts no guard exists ("none ..." forms).

    Only the first word/token decides: "none identified" is True, but a
    guard citation that merely contains "none" later in prose
    ("`test_a.py` (none found)") is False.
    """
    if not isinstance(raw, str):
        raise TypeError(
            f"raw must be str, got {type(raw).__name__}; callers pass str only"
        )
    return _NO_GUARD_RE.match(raw.lstrip()) is not None


def parse_guard_paths(raw: str) -> list[str]:
    """Return candidate test-file references cited by a Guard cell.

    Rules:

    a) a cell whose first word/token is "none" (case-insensitive) yields
       [] -- checked before any token extraction;
    b) backticked tokens (``...``) are extracted; so are bare paths ending
       in .py outside backticks (e.g. mode 51's tests/unit/... form);
    c) a token that is not a .py filename (requirements-dev.txt,
       .github/workflows/ci.yml) yields no candidate;
    d) tokens separated by " + " or "," each yield their own candidate
       (the single finditer pass skips separators on its own);
    e) a trailing parenthetical "(...)" is stripped ONLY when it directly
       follows the filename; its content is dropped, never returned;
    f) duplicates are removed preserving first-occurrence order.

    Returned paths are verbatim (backticks and glued annotations removed,
    nothing prepended or normalized).
    """
    if not isinstance(raw, str):
        raise TypeError(
            f"raw must be str, got {type(raw).__name__}; callers pass str only"
        )
    if not raw.strip():
        return []
    if is_no_guard_note(raw):  # rule (a) before any token extraction
        return []
    candidates: list[str] = []
    for match in _CITED_UNIT.finditer(raw):
        token = (match.group(1) or match.group(2) or "").strip()
        if not token.endswith(".py"):  # rule (c)
            continue
        if token not in candidates:  # rule (f), first-occurrence order
            candidates.append(token)
    return candidates