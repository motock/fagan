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
from pathlib import Path, PurePosixPath

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


def _candidate_basename(candidate: str) -> str:
    """Return the bare filename a cited candidate or collected entry names."""
    return PurePosixPath(candidate.replace("\\", "/")).name


def _collected_instance_key(collected: str) -> str:
    """Normalize one collected entry to the tests/-relative instance path.

    A repo-relative entry (``tests/unit/test_x.py``) names ONE instance:
    ``unit/test_x.py``.  A bare name (``test_x.py``) carries no directory
    information, so it matches every existing instance with that basename
    and normalizes to ``""`` here (the caller falls back to basename
    matching for those).
    """
    parts = [p for p in collected.replace("\\", "/").split("/") if p and p != "."]
    if len(parts) <= 1:
        return ""
    if parts[0] == "tests":
        parts = parts[1:]
    return "/".join(parts)


def _build_tests_index(repo_root: Path) -> dict[str, list[str]]:
    """Map every file basename under ``repo_root/tests`` to its instances.

    Each instance path is relative to the ``tests/`` directory itself
    (``test_x.py``, ``unit/test_x.py``, ``benchmark/test_x.py``).  A missing
    ``tests/`` directory yields an empty index, not an error.  Built once
    per call; nothing is cached between calls.
    """
    tests_dir = repo_root / "tests"
    index: dict[str, list[str]] = {}
    if not tests_dir.is_dir():
        return index
    for path in sorted(tests_dir.rglob("*")):
        if path.is_file():
            index.setdefault(path.name, []).append(
                path.relative_to(tests_dir).as_posix()
            )
    return index


def check_guard_liveness(
    dataset: list[dict],
    repo_root: Path,
    collected_test_files: list[str] | None = None,
) -> dict:
    """Report guard-test liveness for every entry in a failure-mode dataset.

    The dataset arrives as a parameter (the caller reads
    docs/failure_modes.json; this function never opens it).  For each dict
    entry, ``mode``/``status``/``guard`` are copied verbatim, the Guard cell
    is parsed with :func:`parse_guard_paths`, and the report says whether
    each cited test file EXISTS under ``<repo_root>/tests/`` and -- when the
    caller supplies pytest-collected test files -- whether the existing
    files are COLLECTED.

    Report shape::

        {
            "entries": [
                {
                    "mode": ...,            # verbatim from the entry
                    "status": ...,          # verbatim from the entry
                    "guard_note": ...,      # verbatim Guard cell
                    "expected_live": bool,  # "FIXED" in status (case-sensitive)
                    "guard_files": [...],   # parse_guard_paths(guard_note)
                    "missing": [...],       # cited names with no tests/ file
                    "uncollected": [...],   # existing tests/-relative instance
                                            # paths absent from the collected
                                            # list ([] when it is None)
                },
                ...
            ],
            "summary": {
                "total": int,
                "with_guard": int,
                "no_guard_expected": int,
                "missing_files": int,
                "uncollected_files": int,
            },
        }

    Rules:

    - ``expected_live`` is True iff the raw status contains the uppercase
      substring ``"FIXED"`` -- so "FIXED (with 28)" counts but "NOT fixed"
      does not (it only has lowercase "fixed"; no case normalization).
      These are the modes whose guard going dark means a RECURRENCE.
    - Existence is a recursive basename match under ``repo_root/"tests"``;
      a same-named file outside tests/ never counts as found.  A missing
      ``tests/`` directory makes every candidate missing.
    - A dict entry missing ``mode``/``status``/``guard`` still yields a
      record (empty strings, ``expected_live=False``, empty lists) and is
      counted in ``summary["total"]``; non-dict entries are skipped
      entirely (no record, not counted).
    - ``with_guard``/``no_guard_expected`` partition ``total`` by whether
      any guard-file candidate was parsed; ``missing_files``/
      ``uncollected_files`` count ENTRIES with at least one such file, not
      files.  A "none identified" cell parses no candidates, so it can
      never appear in ``missing``.
    - Collected entries may be repo-relative (``tests/x.py``) or bare names
      (``x.py``).  A repo-relative entry collects only the instance it
      names; a bare name collects every existing instance with that
      basename.  Only EXISTING files can be uncollected -- a missing
      candidate is never listed as uncollected.

    Purity: no subprocess, no network, no reads outside the ``tests/``
    walk, no mutation of the inputs, no module-level state -- two identical
    calls return equal dicts.
    """
    if not isinstance(dataset, list):
        raise TypeError(f"dataset must be list[dict], got {type(dataset).__name__}")
    if collected_test_files is not None and not isinstance(collected_test_files, list):
        raise TypeError(
            "collected_test_files must be list[str] or None, got "
            f"{type(collected_test_files).__name__}"
        )

    index = _build_tests_index(repo_root)

    # Collected entries split into bare names (match by basename) and
    # tests/-relative instance paths (match exactly).  Built fresh per call:
    # the function is stateless and never mutates the caller's list.
    collected_basenames: set[str] = set()
    collected_instances: set[str] = set()
    if collected_test_files is not None:
        for collected in collected_test_files:
            if not isinstance(collected, str):
                raise TypeError(
                    "collected_test_files entries must be str, got "
                    f"{type(collected).__name__}"
                )
            instance = _collected_instance_key(collected)
            if instance:
                collected_instances.add(instance)
            else:
                collected_basenames.add(_candidate_basename(collected))

    entries: list[dict] = []
    with_guard = 0
    missing_entries = 0
    uncollected_entries = 0
    for item in dataset:
        if not isinstance(item, dict):
            continue  # non-dict rows: no record, not counted in total
        mode = item.get("mode", "")
        status = item.get("status", "")
        guard_note = item.get("guard", "")
        expected_live = isinstance(status, str) and "FIXED" in status
        guard_files = (
            parse_guard_paths(guard_note) if isinstance(guard_note, str) else []
        )

        missing: list[str] = []
        uncollected: list[str] = []
        for candidate in guard_files:
            basename = _candidate_basename(candidate)
            instances = index.get(basename, [])
            if not instances:
                missing.append(candidate)  # cited verbatim, never normalized
                continue
            if collected_test_files is None:
                continue
            if basename in collected_basenames:
                continue  # a bare collected name collects every instance
            for instance in instances:
                if instance not in collected_instances:
                    uncollected.append(instance)
        uncollected.sort()

        if guard_files:
            with_guard += 1
        if missing:
            missing_entries += 1
        if uncollected:
            uncollected_entries += 1
        entries.append(
            {
                "mode": mode,
                "status": status,
                "guard_note": guard_note,
                "expected_live": expected_live,
                "guard_files": guard_files,
                "missing": missing,
                "uncollected": uncollected,
            }
        )

    return {
        "entries": entries,
        "summary": {
            "total": len(entries),
            "with_guard": with_guard,
            "no_guard_expected": len(entries) - with_guard,
            "missing_files": missing_entries,
            "uncollected_files": uncollected_entries,
        },
    }