"""Regression guard for the bare-basename leak check in the WS-07 suite.

``tests/unit/test_workspace_path_security.py`` defines a helper
``_assert_no_leak`` that scans an error string for internal filesystem
structure.  Its ``secrets`` list used to contain the *bare* repo basename::

    REPO_ROOT.name,

That is a plain substring check, so when the checkout lives in a directory
named ``work`` the legitimate, non-leaking message

    "workspace path is inside a protected system location"

contains ``work`` (inside ``workspace``) and the helper fires.  The same
false positive happens for a checkout named ``path``, ``space``, ``system``
or ``location``.  ``str(REPO_ROOT)`` already covers the full absolute path;
the basename entry was meant to catch the name leaking as a *path segment*,
so the entry must be path-segment shaped::

    f"/{REPO_ROOT.name}/",

These tests pin both halves of that contract:

* POSITIVE - a bare basename appearing as an ordinary English word inside a
  legitimate message must NOT trip the helper (this is the regression guard;
  it fails against the old bare-substring entry).
* NEGATIVE - the basename appearing as a real path segment, the full repo
  path, and every other ``secrets`` entry must STILL trip the helper, so the
  fix cannot be "delete the check".

``mod.REPO_ROOT`` is monkeypatched to a synthetic ``Path`` so the assertions
never depend on what this checkout happens to be named today
(see ``.claude/rules/testing-config-gates.md``: stub the source, never assert
against today's real value).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

# The existing suite this file guards.  Imported as a sibling test module the
# same way tests/unit/test_routing_registry_isolation.py imports
# tests.unit.test_routing_registry; the importlib fallback keeps the file
# runnable if the namespace-package import path ever stops resolving.
_EXISTING_TEST_FILE = Path(__file__).with_name("test_workspace_path_security.py")

try:  # pragma: no cover - exercised implicitly by every test below
    from tests.unit import test_workspace_path_security as mod
except ImportError:  # pragma: no cover
    _spec = importlib.util.spec_from_file_location(
        "_ws_path_security_mod_under_test", _EXISTING_TEST_FILE
    )
    assert _spec is not None and _spec.loader is not None
    mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(mod)


# Synthetic repo roots: the basename is the thing under test, so it must be
# supplied by the test rather than read off the real checkout.
WORK_ROOT = Path("/tmp/work")
PATH_ROOT = Path("/tmp/path")

# Every basename that used to produce a false positive because it is also an
# ordinary English word.
FALSE_POSITIVE_BASENAMES = ("work", "path", "space", "system", "location")

# The exact legitimate message from the bug report.
LEGITIMATE_MESSAGE = "workspace path is inside a protected system location"


# ---------------------------------------------------------------------------
# 0. The helper under test is reachable and monkeypatchable
# ---------------------------------------------------------------------------


class TestHelperIsReachable:
    def test_helper_is_module_level_and_callable(self):
        assert callable(mod._assert_no_leak)

    def test_repo_root_is_a_module_level_path(self):
        # The helper reads REPO_ROOT from module globals at call time, which
        # is what makes the monkeypatching below meaningful.
        assert isinstance(mod.REPO_ROOT, Path)

    def test_monkeypatched_repo_root_is_what_the_helper_reads(
        self, monkeypatch
    ):
        # Proves the helper consults the module global rather than a value
        # captured at import time: the same text flips from leaking to clean
        # purely because REPO_ROOT changed.
        monkeypatch.setattr(mod, "REPO_ROOT", WORK_ROOT)
        with pytest.raises(AssertionError):
            mod._assert_no_leak("/tmp/work")

        monkeypatch.setattr(mod, "REPO_ROOT", Path("/tmp/elsewhere"))
        mod._assert_no_leak("/tmp/work")  # no longer the repo root


# ---------------------------------------------------------------------------
# 1-2. POSITIVE: a bare basename as an English word must not raise
# ---------------------------------------------------------------------------


class TestBareBasenameIsNotTreatedAsALeak:
    def test_legitimate_message_does_not_raise_with_work_repo_root(
        self, monkeypatch
    ):
        # The headline regression: 'work' is a substring of 'workspace'.
        monkeypatch.setattr(mod, "REPO_ROOT", WORK_ROOT)
        mod._assert_no_leak(LEGITIMATE_MESSAGE)

    def test_path_basename_message_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(mod, "REPO_ROOT", PATH_ROOT)
        mod._assert_no_leak("path is not allowed")

    @pytest.mark.parametrize("basename", FALSE_POSITIVE_BASENAMES)
    def test_every_english_word_basename_is_tolerated(
        self, monkeypatch, basename
    ):
        monkeypatch.setattr(mod, "REPO_ROOT", Path(f"/tmp/{basename}"))
        mod._assert_no_leak(f"{basename} is not allowed")

    def test_basename_embedded_in_a_longer_word_is_tolerated(
        self, monkeypatch
    ):
        monkeypatch.setattr(mod, "REPO_ROOT", WORK_ROOT)
        mod._assert_no_leak("the workspace is not a network share")

    def test_empty_text_does_not_raise(self, monkeypatch):
        # Boundary: empty input carries no structure at all.
        monkeypatch.setattr(mod, "REPO_ROOT", WORK_ROOT)
        mod._assert_no_leak("")

    def test_extra_secrets_empty_list_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(mod, "REPO_ROOT", WORK_ROOT)
        mod._assert_no_leak("ok", extra_secrets=[])

    def test_extra_secrets_none_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(mod, "REPO_ROOT", WORK_ROOT)
        mod._assert_no_leak("ok", extra_secrets=None)

    def test_extra_secret_absent_from_text_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(mod, "REPO_ROOT", WORK_ROOT)
        mod._assert_no_leak("ok", extra_secrets=["zzz"])

    def test_extra_secrets_absent_from_text_do_not_raise(self, monkeypatch):
        monkeypatch.setattr(mod, "REPO_ROOT", WORK_ROOT)
        mod._assert_no_leak("nothing here", extra_secrets=["zzz", "qqq"])


# ---------------------------------------------------------------------------
# 3-6. NEGATIVE: real leaks must STILL raise AssertionError
# ---------------------------------------------------------------------------


class TestRealLeaksStillRaise:
    def test_basename_as_a_path_segment_still_raises(self, monkeypatch):
        monkeypatch.setattr(mod, "REPO_ROOT", WORK_ROOT)
        with pytest.raises(AssertionError) as excinfo:
            mod._assert_no_leak("failed at /tmp/work/pipeline/x")
        assert "leaked internal detail" in str(excinfo.value)
        assert "/work/" in str(excinfo.value)

    def test_full_repo_root_still_raises(self, monkeypatch):
        monkeypatch.setattr(mod, "REPO_ROOT", WORK_ROOT)
        with pytest.raises(AssertionError) as excinfo:
            mod._assert_no_leak("/tmp/work")
        assert "leaked internal detail" in str(excinfo.value)
        assert "/tmp/work" in str(excinfo.value)

    def test_trailing_slash_path_segment_still_raises(self, monkeypatch):
        monkeypatch.setattr(mod, "REPO_ROOT", WORK_ROOT)
        with pytest.raises(AssertionError):
            mod._assert_no_leak("denied: /tmp/work/")

    def test_path_basename_as_a_path_segment_still_raises(self, monkeypatch):
        monkeypatch.setattr(mod, "REPO_ROOT", PATH_ROOT)
        with pytest.raises(AssertionError):
            mod._assert_no_leak("failed at /tmp/path/pipeline/x")

    def test_traceback_entry_is_untouched(self, monkeypatch):
        monkeypatch.setattr(mod, "REPO_ROOT", WORK_ROOT)
        with pytest.raises(AssertionError) as excinfo:
            mod._assert_no_leak("Traceback (most recent call last)")
        assert "Traceback" in str(excinfo.value)

    def test_py_suffix_entry_is_untouched(self, monkeypatch):
        monkeypatch.setattr(mod, "REPO_ROOT", WORK_ROOT)
        with pytest.raises(AssertionError):
            mod._assert_no_leak("boom at /x/y.py:42")

    def test_site_packages_entry_is_untouched(self, monkeypatch):
        monkeypatch.setattr(mod, "REPO_ROOT", WORK_ROOT)
        with pytest.raises(AssertionError):
            mod._assert_no_leak("imported from site-packages/foo")

    def test_extra_secret_present_in_text_still_raises(self, monkeypatch):
        monkeypatch.setattr(mod, "REPO_ROOT", WORK_ROOT)
        with pytest.raises(AssertionError) as excinfo:
            mod._assert_no_leak("has zzz", extra_secrets=["zzz"])
        assert "zzz" in str(excinfo.value)

    def test_extra_secret_is_checked_alongside_the_defaults(self, monkeypatch):
        # Both the default entries and the caller-supplied ones are scanned
        # in the same call: 'Traceback' is a default, 'zzz' is an extra.
        monkeypatch.setattr(mod, "REPO_ROOT", WORK_ROOT)
        with pytest.raises(AssertionError):
            mod._assert_no_leak("Traceback has zzz", extra_secrets=["zzz"])


# ---------------------------------------------------------------------------
# Source-level pins for the one-line edit (mechanically checkable criteria)
# ---------------------------------------------------------------------------


class TestSecretsListSourceShape:
    def _source(self) -> str:
        return _EXISTING_TEST_FILE.read_text(encoding="utf-8")

    def test_bare_basename_entry_is_gone(self):
        # Criterion A: `grep -n 'REPO_ROOT.name,'` must return no match.
        source = self._source()
        assert not re.search(r"^\s*REPO_ROOT\.name,\s*$", source, re.MULTILINE), (
            "the bare REPO_ROOT.name substring entry must be removed from "
            "the secrets list"
        )

    def test_path_segment_entry_present_exactly_once(self):
        # Criterion B: `grep -n 'f"/{REPO_ROOT.name}/",'` must match once.
        source = self._source()
        matches = re.findall(
            r'^\s*f"/\{REPO_ROOT\.name\}/",\s*$', source, re.MULTILINE
        )
        assert len(matches) == 1, (
            "the secrets list must contain exactly one path-segment-shaped "
            f"basename entry, found {len(matches)}"
        )

    def test_full_repo_root_entry_is_still_present(self):
        # Membership, not exact contents: later stories may extend the list.
        source = self._source()
        assert re.search(r"^\s*str\(REPO_ROOT\),\s*$", source, re.MULTILINE)

    def test_other_secrets_entries_are_still_present(self):
        source = self._source()
        for entry in ('"Traceback",', '".py",', '"site-packages",'):
            assert entry in source, f"secrets entry {entry} was removed"

    def test_assertion_message_is_unchanged(self):
        source = self._source()
        assert "leaked internal detail" in source

    def test_helper_signature_is_unchanged(self):
        source = self._source()
        assert (
            "def _assert_no_leak(text: str, extra_secrets: list[str] | None = None)"
            in source
        )


class TestGuardedTestsStillExist:
    """The two tests that failed on a clean tree must not be deleted."""

    def test_traversal_leak_test_still_exists(self):
        cls = getattr(mod, "TestTraversalVariantsDenied", None)
        assert cls is not None
        assert hasattr(cls, "test_traversal_error_does_not_leak_resolved_path")

    def test_normalize_error_leak_test_still_exists(self):
        cls = getattr(mod, "TestErrorHygiene", None)
        assert cls is not None
        assert hasattr(cls, "test_normalize_error_does_not_leak_repo_root")
