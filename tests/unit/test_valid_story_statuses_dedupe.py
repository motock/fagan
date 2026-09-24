"""Dedupe tests for the story-status allowlist (MCPHYG-2).

``_VALID_STORY_STATUSES`` was hand-duplicated: ``pipeline/ci.py`` and
``pipeline/service.py`` each defined their own frozenset, and both listed
``"done"`` twice. ``pipeline/server.py`` imports the name FROM ``pipeline.ci``,
so ci.py's copy is canonical; service.py must resolve the same object lazily
through its ``_ServerRef`` idiom (a plain cross-module import would be circular:
server.py imports ``_VALID_STORY_STATUSES`` from ci.py at load time).

These tests fail until the duplicate is removed and service.py delegates.
"""

import pipeline.ci as pci
import pipeline.server as pserver
import pipeline.service as pservice


def test_ci_allowlist_lists_done_exactly_once():
    """The canonical allowlist must not repeat "done"."""
    assert list(pci._VALID_STORY_STATUSES).count("done") == 1, (
        f"expected exactly one 'done' entry, got "
        f"{list(pci._VALID_STORY_STATUSES)!r}"
    )


def test_ci_allowlist_source_lists_done_once():
    """A frozenset silently dedupes, so the runtime count cannot catch the
    hand-written duplicate: assert the *source literal* lists "done" once."""
    import inspect
    import re

    src = inspect.getsource(pci)
    match = re.search(
        r"_VALID_STORY_STATUSES = frozenset\((.*?)\n\)", src, re.DOTALL
    )
    assert match, "could not locate the _VALID_STORY_STATUSES literal in ci.py"
    assert match.group(1).count('"done"') == 1, (
        "ci.py's _VALID_STORY_STATUSES literal must list 'done' exactly once; "
        f"found {match.group(1).count(chr(34) + 'done' + chr(34))}"
    )


def test_service_allowlist_resolves_to_same_object_as_ci():
    """service.py's binding must resolve to ci.py's exact frozenset object
    (not a second hand-duplicated copy)."""
    resolved = pservice._VALID_STORY_STATUSES._value()
    assert resolved is pci._VALID_STORY_STATUSES, (
        "pipeline.service._VALID_STORY_STATUSES must resolve to the same "
        "frozenset object as pipeline.ci._VALID_STORY_STATUSES"
    )


def test_server_allowlist_is_the_ci_object():
    """server.py imports the name from ci.py, so it must be the same object."""
    assert pserver._VALID_STORY_STATUSES is pci._VALID_STORY_STATUSES


def test_service_allowlist_contains_expected_statuses():
    """The resolved allowlist still contains the expected statuses."""
    resolved = pservice._VALID_STORY_STATUSES._value()
    assert list(resolved).count("done") == 1
    for status in ("todo", "in_progress", "running", "parked", "done"):
        assert status in resolved


def test_service_allowlist_is_not_a_second_frozenset_literal():
    """service.py must not define its own frozenset literal any more."""
    import inspect

    src = inspect.getsource(pservice)
    assert "_VALID_STORY_STATUSES = frozenset(" not in src, (
        "service.py must not hand-duplicate the allowlist; use "
        '_ServerRef("_VALID_STORY_STATUSES")'
    )
