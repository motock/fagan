"""OA2-09: the shipped launchd template enables PIPELINE_AUTO_TRIAGE.

The committed plist is generated from the template, so both must carry the
key with the same value; a drift between them is a real bug.
"""

import plistlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHD = REPO_ROOT / "launchd"
TEMPLATE = LAUNCHD / "com.fagan.pipeline.advance-scheduler.plist.template"
COMMITTED = LAUNCHD / "com.fagan.pipeline.advance-scheduler.plist"


def _env(path: Path) -> dict:
    with path.open("rb") as fh:
        return plistlib.load(fh)["EnvironmentVariables"]


def test_template_enables_auto_triage():
    assert _env(TEMPLATE)["PIPELINE_AUTO_TRIAGE"] == "1"


def test_committed_plist_matches_template_auto_triage():
    assert _env(COMMITTED)["PIPELINE_AUTO_TRIAGE"] == "1"
