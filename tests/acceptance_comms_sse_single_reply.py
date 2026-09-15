"""Read-only acceptance oracle: one chat turn renders exactly ONE Tower
bubble from the server's ``turn`` -> ``reply`` -> ``result`` frame pair.

Not isolation-only: it drives the REAL client through the existing
frontend suite, which boots the shipped ``static/app/comms.js`` against
its DOM stubs and counts thread children. Before the duplicate-reply fix
that count was 2 -- the ``reply`` and ``result`` frames each appended a
bubble, because ``renderFinal`` had no element to reuse when no tool call
had created one. A green run here means the shipped client reuses a
single bubble for both frames.

The ``>= 36`` bound is a deliberate ratchet, not an exact-count
enumeration: the suite held 35 passing tests when the duplicate was
found, and none of them exercised the real frame pair. Requiring one more
proves the regression case was actually added and passes. Later stories
may append further cases without breaking this file; nothing here pins a
total, a file hash, or an exact test name.
"""
import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SUITE = "tests/unit/test_comms_sse_stream.mjs"

SUMMARY_RE = re.compile(r"(\d+)/(\d+) passed")
PRE_FIX_TEST_COUNT = 35


def test_comms_sse_suite_passes_and_pins_the_single_reply_regression():
    assert shutil.which("node"), "node is required to run the frontend suite"

    proc = subprocess.run(
        ["node", SUITE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"{SUITE} failed:\n{combined}"
    assert "FAIL" not in proc.stdout, f"the suite reported failures:\n{combined}"

    summaries = SUMMARY_RE.findall(proc.stdout)
    assert summaries, f"no '<passed>/<total> passed' summary line in:\n{combined}"
    passed, total = (int(n) for n in summaries[-1])
    assert passed == total, f"only {passed}/{total} passed:\n{combined}"
    assert passed >= PRE_FIX_TEST_COUNT + 1, (
        f"only {passed} tests passed; the duplicate-reply regression case "
        "(turn + reply + result -> exactly one tower bubble) must exist and pass"
    )
