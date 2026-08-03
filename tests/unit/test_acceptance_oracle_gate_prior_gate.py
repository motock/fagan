"""Acceptance oracle: a fixture whose own test edits trip the
``confirm_removals`` deletion gate (already merged on master) is BORN BROKEN -
no implementation can satisfy it, because the gate blocks the oracle's own
edits independent of the feature under test (Mode 49: 5 wasted dispatches
that looked like model read-loops but were an unwinnable oracle).

The pre-dispatch gate must catch this BEFORE an implementer is launched. The
detection turns on a discriminator that separates an *accidental* gate trip
from a *legitimate* gate test:

* Mode 49's oracle tested ``expect_first``/``expect_last`` anchors and
  accidentally used content-rewrite edits the gate blocks - its source never
  mentions ``confirm_removals``.
* A legitimate oracle that INTENDS to test the gate (e.g. the s2
  ``replace_lines_gate`` fixture, which references ``confirm_removals`` seven
  times) must still dispatch normally.

So: when the oracle's failure output carries the gate's block signature AND
none of the acceptance sources reference ``confirm_removals``, reclassify the
otherwise-``fails_correctly`` outcome as ``errors`` (born-broken) so the
dispatch path blocks it.
"""
import types

from pipeline import oracle_gate

# The exact block signature emitted by scripts/local_agent.py's replace_lines
# when confirm_removals rejects an edit - kept in sync with that file.
_BLOCK = "repeat this exact call with confirm_removals=true"


def _story(tmp_path, source):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    return {
        "summary": "s",
        "acceptance": [{"path": "tests/unit/test_zz_fake_oracle.py", "source": source}],
    }


def _fake_run(output, returncode=1):
    return types.SimpleNamespace(returncode=returncode, stdout=output, stderr="")


def test_accidental_gate_trip_is_born_broken(tmp_path, monkeypatch):
    # An oracle testing anchors - its source never mentions confirm_removals,
    # but its content-rewrite edits trip the gate at baseline.
    source = "def test_anchor():\n    assert expect_first is not None\n"
    output = (
        "ERROR: this edit to f.py deletes 1 line(s) that don't appear to "
        f"survive\n\nRevise new_str, or if the deletion is intentional, "
        f"{_BLOCK}. The edit was NOT applied.\n1 failed"
    )
    monkeypatch.setattr(oracle_gate.subprocess, "run", lambda *a, **k: _fake_run(output))
    result = oracle_gate.validate_acceptance_fixtures(_story(tmp_path, source), tmp_path)
    assert result["state"] == "errors"
    assert "confirm_removals" in result["detail"] or "prior gate" in result["detail"]


def test_legitimate_gate_test_still_dispatches(tmp_path, monkeypatch):
    # An oracle that INTENDS to test the gate references confirm_removals in
    # its source - a baseline failure here is the feature being unimplemented,
    # not a born-broken oracle, even though the block signature appears.
    source = (
        "def test_gate_blocks():\n"
        "    assert 'confirm_removals' in schema\n"
    )
    output = f"... {_BLOCK}. The edit was NOT applied.\n1 failed"
    monkeypatch.setattr(oracle_gate.subprocess, "run", lambda *a, **k: _fake_run(output))
    result = oracle_gate.validate_acceptance_fixtures(_story(tmp_path, source), tmp_path)
    assert result["state"] == "fails_correctly"


def test_no_gate_signature_is_unaffected(tmp_path, monkeypatch):
    # A plain missing-feature failure never mentions the gate - stays
    # fails_correctly as before.
    source = "def test_x():\n    assert feature()\n"
    monkeypatch.setattr(
        oracle_gate.subprocess, "run", lambda *a, **k: _fake_run("E  assert 0")
    )
    result = oracle_gate.validate_acceptance_fixtures(_story(tmp_path, source), tmp_path)
    assert result["state"] == "fails_correctly"


def test_gate_trip_with_no_acceptance_source_is_born_broken(tmp_path, monkeypatch):
    # Defensive: if a fixture entry somehow lacks source, the discriminator
    # cannot prove the oracle intends to test the gate, so treat as born-broken.
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    story = {
        "summary": "s",
        "acceptance": [{"path": "tests/unit/test_zz_fake_oracle.py"}],  # no source
    }
    output = f"{_BLOCK}. The edit was NOT applied.\n1 failed"
    monkeypatch.setattr(oracle_gate.subprocess, "run", lambda *a, **k: _fake_run(output))
    result = oracle_gate.validate_acceptance_fixtures(story, tmp_path)
    assert result["state"] == "errors"