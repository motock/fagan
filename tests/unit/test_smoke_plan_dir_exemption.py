"""The getting-started smoke script documents why it is exempt from the PLAN_DIR rule."""

from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "smoke_getting_started.py"


def test_smoke_script_states_its_plan_dir_exemption_before_its_imports():
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    markers = [i for i, line in enumerate(lines) if line.startswith("# PLAN_DIR exemption:")]
    first_import = next(i for i, line in enumerate(lines) if line.startswith("import "))

    assert len(markers) == 1
    assert markers[0] < first_import
