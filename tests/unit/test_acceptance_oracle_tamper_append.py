"""Acceptance oracle: in a non-TDD-split story the implementer is instructed
"do not modify any existing test; add new tests" and legitimately APPENDS
tests to the oracle fixture. The merge-gate tamper check must not refuse a
pure append - the original grader is byte-intact as a prefix, so its
authority over the original behavior is preserved; only new (non-authoritative)
tests were added after it.

Hit live on edit-guard-enforcement s4 (PR #222): the implementer appended 136
lines to a 105-line oracle; ``approve_merge`` rejected a pure ``105a106,241``
append as "acceptance fixture modified since dispatch".

Rules the gate must enforce after this fix:
* non-TDD-split story + pure append (original source is a byte-exact prefix of
  the worktree file) -> NOT tampered.
* non-TDD-split story + mid-file rewrite (original not a prefix) -> tampered.
* TDD-split story + any change at all (oracle is read-only) -> tampered.
* missing fixture -> tampered regardless (deleting the oracle must not pass).
"""
import hashlib

from pipeline import ci

SOURCE = "def test_x():\n    assert True\n"
DIGEST = hashlib.sha256(SOURCE.encode()).hexdigest()

_APPENDED = SOURCE + "def test_new():\n    assert 1 + 1 == 2\n"
_MIDFILE = "def test_x():\n    assert False\n"  # original line rewritten


def _story(tdd_split=False):
    return {
        "tdd_split": tdd_split,
        "acceptance": [{"path": "oracle.py", "source": SOURCE}],
        "acceptance_digests": {"oracle.py": DIGEST},
    }


def test_nontdd_pure_append_is_not_tampered(tmp_path):
    (tmp_path / "oracle.py").write_text(_APPENDED)
    assert ci._acceptance_tampered(_story(tdd_split=False), str(tmp_path)) == []


def test_nontdd_midfile_rewrite_is_tampered(tmp_path):
    (tmp_path / "oracle.py").write_text(_MIDFILE)
    assert ci._acceptance_tampered(_story(tdd_split=False), str(tmp_path)) == ["oracle.py"]


def test_tdd_split_pure_append_is_tampered(tmp_path):
    # In TDD-split flow the oracle is read-only - even a pure append is refused.
    (tmp_path / "oracle.py").write_text(_APPENDED)
    assert ci._acceptance_tampered(_story(tdd_split=True), str(tmp_path)) == ["oracle.py"]


def test_tdd_split_untouched_is_not_tampered(tmp_path):
    (tmp_path / "oracle.py").write_text(SOURCE)
    assert ci._acceptance_tampered(_story(tdd_split=True), str(tmp_path)) == []


def test_nontdd_missing_fixture_is_tampered(tmp_path):
    assert ci._acceptance_tampered(_story(tdd_split=False), str(tmp_path)) == ["oracle.py"]


def test_nontdd_append_with_trailing_whitespace_difference_is_tampered(tmp_path):
    # A prefix match is byte-exact; altering the original region at all breaks
    # the prefix, so this is tampered even in non-TDD-split.
    altered_prefix = "def test_x():\n    assert True \n"  # trailing space added
    (tmp_path / "oracle.py").write_text(altered_prefix + "def test_new():\n    pass\n")
    assert ci._acceptance_tampered(_story(tdd_split=False), str(tmp_path)) == ["oracle.py"]