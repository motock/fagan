"""Malformed ``acceptance`` must be a lint FINDING, never a traceback.

Root cause (2026-09-15): ``pipeline.build_detect._lint_acceptance_fixtures``
did ``e.get("path")`` for every element of ``story["acceptance"]``, assuming
each element is a dict.  A plan author who writes acceptance as a list of
criteria STRINGS (``.claude/rules/pipeline-story-schema.md``: "acceptance is an
array of {path, source} file fixtures, not a list of strings") made the helper
raise ``AttributeError: 'str' object has no attribute 'get'``, which propagated
out of ``ingest_plan`` as a raw traceback.  Reproduced live by
``scripts/smoke_getting_started.py``, whose own plan uses string acceptance -
the entire documented getting-started path died at ingest.

Contract pinned here: the helper NEVER raises on a malformed acceptance value.
It returns ``("finding", message)`` so the gate that already exists in
``pipeline/ingest.py`` (``if kind == "finding": return {"ok": False, "error":
lint_msg}``) rejects the plan with an actionable error instead of a traceback.
The message names the story summary, identifies the malformed entry, and states
the required shape (a list of ``{"path": ..., "source": ...}`` dicts).

Per-story test file: the sibling ``test_acceptance_fixture_lint.py`` is shared
with other stories and must not be edited by this one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pipeline.build_detect import _lint_acceptance_fixtures

CLEAN_SOURCE = "def test_x():\n    assert True\n"
F401_SOURCE = "import os\n\n\ndef test_x():\n    assert True\n"

# The message must state the required shape.  Any of these phrasings satisfies
# "a list of {"path": ..., "source": ...} dicts" without pinning one wording.
_SHAPE_KEYWORDS = (
    "malformed",
    "not a list",
    "non-dict",
    "list of strings",
    "list of dicts",
    "shape",
)


def _story(entries, summary="s"):
    return {"summary": summary, "acceptance": entries}


def _assert_finding(result, summary):
    """A malformed acceptance value must be a 2-tuple finding, not an exception."""
    assert isinstance(result, tuple), f"expected a tuple, got {result!r}"
    assert len(result) == 2, f"expected a (kind, message) pair, got {result!r}"
    kind, msg = result
    assert kind == "finding", f"expected 'finding', got {kind!r} (msg={msg!r})"
    assert isinstance(msg, str) and msg, f"expected a non-empty message, got {msg!r}"
    assert summary in msg, f"message must name the story summary: {msg!r}"
    assert "acceptance" in msg.lower(), (
        f"message must identify the malformed acceptance value: {msg!r}"
    )
    assert "path" in msg and "source" in msg, (
        f"message must state the required {{'path': ..., 'source': ...}} shape: {msg!r}"
    )
    assert any(kw in msg.lower() for kw in _SHAPE_KEYWORDS), (
        f"message must say the acceptance shape is wrong: {msg!r}"
    )
    return kind, msg


# --------------------------------------------------------------------------
# 1. POSITIVE - the bug: acceptance as a list of criteria strings
# --------------------------------------------------------------------------


def test_string_acceptance_entries_return_finding_not_attributeerror():
    summary = "getting-started-string-acceptance"
    story = _story(["some criterion", "another"], summary=summary)

    _kind, msg = _assert_finding(_lint_acceptance_fixtures(story), summary)

    assert "some criterion" in msg, (
        f"message must identify the malformed entry: {msg!r}"
    )


def test_string_acceptance_entries_never_raise_even_with_repo_root():
    summary = "string-acceptance-with-repo-root"
    story = _story(["a criterion"], summary=summary)

    _kind, msg = _assert_finding(
        _lint_acceptance_fixtures(story, "/tmp/some-repo-root"), summary
    )

    assert "a criterion" in msg


# --------------------------------------------------------------------------
# 2. MIXED - one bad entry among well-formed dicts is enough
# --------------------------------------------------------------------------


def test_mixed_dict_and_string_entries_return_finding():
    summary = "mixed-acceptance-shapes"
    story = _story(
        [
            {"path": "tests/t.py", "source": "x = 1\n"},
            "a string",
        ],
        summary=summary,
    )

    _kind, msg = _assert_finding(_lint_acceptance_fixtures(story), summary)

    assert "a string" in msg, (
        f"message must identify the malformed entry: {msg!r}"
    )


def test_mixed_entries_do_not_run_ruff(monkeypatch):
    """A malformed shape is rejected before any lint subprocess is spawned."""
    calls = []
    monkeypatch.setattr(
        "pipeline.build_detect.subprocess.run", lambda *a, **k: calls.append(1)
    )

    story = _story([{"path": "tests/t.py", "source": "x = 1\n"}, "a string"])
    kind, _msg = _lint_acceptance_fixtures(story)

    assert kind == "finding"
    assert calls == []


def test_none_entry_among_dicts_returns_finding():
    """A non-dict, non-string entry (None) is malformed too."""
    summary = "none-acceptance-entry"
    story = _story([{"path": "tests/t.py", "source": "x = 1\n"}, None], summary=summary)

    _assert_finding(_lint_acceptance_fixtures(story), summary)


# --------------------------------------------------------------------------
# 3. NOT-A-LIST - acceptance itself is the wrong container type
# --------------------------------------------------------------------------


def test_acceptance_as_bare_string_returns_finding():
    summary = "acceptance-is-a-string"
    story = _story("a string", summary=summary)

    _assert_finding(_lint_acceptance_fixtures(story), summary)


def test_acceptance_as_dict_returns_finding():
    summary = "acceptance-is-a-dict"
    story = _story({"path": "x"}, summary=summary)

    _assert_finding(_lint_acceptance_fixtures(story), summary)


def test_acceptance_as_int_returns_finding():
    summary = "acceptance-is-an-int"
    story = _story(7, summary=summary)

    _assert_finding(_lint_acceptance_fixtures(story), summary)


def test_malformed_acceptance_does_not_run_ruff(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "pipeline.build_detect.subprocess.run", lambda *a, **k: calls.append(1)
    )

    for bad in ("a string", {"path": "x"}):
        kind, _msg = _lint_acceptance_fixtures(_story(bad))
        assert kind == "finding"

    assert calls == []


# --------------------------------------------------------------------------
# 4. NEGATIVE CONTROL - the unchanged clean path
# --------------------------------------------------------------------------


def test_empty_acceptance_list_is_still_clean(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "pipeline.build_detect.subprocess.run", lambda *a, **k: calls.append(1)
    )

    kind, msg = _lint_acceptance_fixtures(_story([]))

    assert (kind, msg) == ("clean", None)
    assert calls == []


def test_missing_acceptance_key_is_still_clean(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "pipeline.build_detect.subprocess.run", lambda *a, **k: calls.append(1)
    )

    kind, msg = _lint_acceptance_fixtures({"summary": "s"})

    assert (kind, msg) == ("clean", None)
    assert calls == []


def test_acceptance_explicitly_none_is_still_clean(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "pipeline.build_detect.subprocess.run", lambda *a, **k: calls.append(1)
    )

    kind, msg = _lint_acceptance_fixtures(_story(None))

    assert (kind, msg) == ("clean", None)
    assert calls == []


def test_non_python_dict_fixture_is_still_clean(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "pipeline.build_detect.subprocess.run", lambda *a, **k: calls.append(1)
    )

    story = _story([{"path": "docs/notes.md", "source": "# notes\n"}])
    kind, msg = _lint_acceptance_fixtures(story)

    assert (kind, msg) == ("clean", None)
    assert calls == []


# --------------------------------------------------------------------------
# 5. REGRESSION - the all-dict .py path behaves exactly as it does today
# --------------------------------------------------------------------------


def test_all_dict_py_fixture_clean_path_unchanged(monkeypatch):
    written = {}

    def fake_which(name):
        return "/usr/bin/ruff"

    def fake_run(cmd, **kwargs):
        # the scratch dir is a TemporaryDirectory, so it only exists for the
        # duration of this call - read the materialized file here
        written_file = Path(cmd[-1]) / "tests/unit/test_x.py"
        written["source"] = written_file.read_text()
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("pipeline.build_detect.shutil.which", fake_which)
    monkeypatch.setattr("pipeline.build_detect.subprocess.run", fake_run)

    story = _story([{"path": "tests/unit/test_x.py", "source": CLEAN_SOURCE}])
    kind, msg = _lint_acceptance_fixtures(story)

    assert (kind, msg) == ("clean", None)
    assert written["source"] == CLEAN_SOURCE


def test_all_dict_py_fixture_violation_path_unchanged(monkeypatch):
    sample_output = "tests/unit/test_x.py:1:8: F401 [*] `os` imported but unused"

    def fake_which(name):
        return "/usr/bin/ruff"

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, returncode=1, stdout=sample_output, stderr=""
        )

    monkeypatch.setattr("pipeline.build_detect.shutil.which", fake_which)
    monkeypatch.setattr("pipeline.build_detect.subprocess.run", fake_run)

    story = _story(
        [{"path": "tests/unit/test_x.py", "source": F401_SOURCE}], summary="my-story"
    )
    kind, msg = _lint_acceptance_fixtures(story)

    assert kind == "finding"
    assert "tests/unit/test_x.py" in msg
    assert "ruff" in msg
    assert "my-story" in msg


def test_all_dict_py_fixture_missing_ruff_is_still_skipped(monkeypatch):
    calls = []
    monkeypatch.setattr("pipeline.build_detect.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "pipeline.build_detect.subprocess.run", lambda *a, **k: calls.append(1)
    )

    story = _story([{"path": "tests/unit/test_x.py", "source": CLEAN_SOURCE}])
    kind, msg = _lint_acceptance_fixtures(story)

    assert kind == "skipped"
    assert msg is not None
    assert calls == []


# --------------------------------------------------------------------------
# Docstring: the enumerated return kinds must cover the malformed-shape case
# --------------------------------------------------------------------------


def test_docstring_documents_malformed_shape_as_a_finding():
    doc = _lint_acceptance_fixtures.__doc__ or ""
    lowered = doc.lower()

    assert "finding" in lowered, "docstring must still enumerate the finding kind"
    assert any(kw in lowered for kw in _SHAPE_KEYWORDS), (
        "docstring's 'finding' kind must cover the malformed acceptance shape "
        f"alongside a ruff violation; got:\n{doc}"
    )
