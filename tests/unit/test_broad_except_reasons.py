"""Every `noqa` naming BLE001 must carry a reason: ` - ` plus at least three words."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("pipeline", "app", "scripts")
MIN_REASON_WORDS = 3

_NOQA_BLE001 = re.compile(r"#\s*noqa:\s*(?:[A-Z]+[0-9]+\s*,\s*)*BLE001(?:\s*,\s*[A-Z]+[0-9]+)*(?P<rest>.*)$")
_REASON = re.compile(r"\s*-\s+(?P<reason>\S.*)$")


def has_required_reason(line: str) -> bool:
    """True when ``line`` has no BLE001 noqa, or one followed by `` - <3+ words>``."""
    match = _NOQA_BLE001.search(line)
    if match is None:
        return True
    reason = _REASON.match(match.group("rest"))
    return reason is not None and len(reason.group("reason").split()) >= MIN_REASON_WORDS


def test_every_production_ble001_noqa_has_a_reason():
    offenders = []
    for directory in SCAN_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not has_required_reason(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert offenders == [], f"BLE001 noqa without ' - <reason of 3+ words>': {offenders}"


def test_bare_noqa_fails():
    assert not has_required_reason("except Exception:  # noqa: BLE001")


def test_one_word_reason_fails():
    assert not has_required_reason("except Exception:  # noqa: BLE001 - x")


def test_two_word_reason_fails():
    assert not has_required_reason("except Exception:  # noqa: BLE001 - never blocks")


def test_reason_without_dash_fails():
    assert not has_required_reason("except Exception:  # noqa: BLE001 (never blocks the tick)")


def test_three_word_reason_passes():
    assert has_required_reason("except Exception:  # noqa: BLE001 - never blocks the tick")


def test_two_codes_with_reason_passes():
    assert has_required_reason("except Exception:  # noqa: S110, BLE001 - never blocks the tick")


def test_two_codes_without_reason_fails():
    assert not has_required_reason("except Exception:  # noqa: S110, BLE001")


def test_line_without_ble001_passes():
    assert has_required_reason("import os  # noqa: F401")
