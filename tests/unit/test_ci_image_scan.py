"""Guards for the Trivy image scan in ci.yml's `image` job.

The scan reports the CVE state of the image this repo actually ships, on every
pull request. These guards pin its presence, its SHA pin, its severity, its
report-only `exit-code`, and its position after the build. PyYAML is not a
dependency of this repo, so ci.yml is read as text.
"""

from __future__ import annotations

import re
from pathlib import Path

CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"

SHA = re.compile(r"[0-9a-f]{40}")
BUILD = "docker build -f deploy/Dockerfile"


def _text() -> str:
    return CI.read_text(encoding="utf-8")


def test_ci_scans_the_built_image() -> None:
    assert "aquasecurity/trivy-action@" in _text()


def test_the_scan_action_is_pinned_to_a_full_sha() -> None:
    match = re.search(r"aquasecurity/trivy-action@(\S+)", _text())
    assert match is not None, "ci.yml no longer runs aquasecurity/trivy-action"
    assert SHA.fullmatch(match.group(1)) is not None, (
        f"the scan action must be pinned to a 40-char SHA, found {match.group(1)!r}"
    )


def test_the_scan_covers_high_and_critical() -> None:
    assert "severity: HIGH,CRITICAL" in _text()


def test_the_scan_is_report_only_for_now() -> None:
    # Deliberate first landing: the base image and pinned operator tooling
    # carry HIGH findings no dependency change in this repo can clear, so a
    # failing threshold on day one would red every pull request. Flipping
    # this to '1' is a separate, later change once the baseline is triaged.
    assert "exit-code: '0'" in _text()


def test_the_scan_ignores_unfixed_findings() -> None:
    assert "ignore-unfixed: true" in _text(), (
        "an unfixable base-image CVE would otherwise block every pull request"
    )


def test_the_scan_runs_after_the_image_is_built() -> None:
    text = _text()
    assert BUILD in text, "PR CI must still build deploy/Dockerfile"
    assert text.index(BUILD) < text.index("aquasecurity/trivy-action@"), (
        "the scan must grade the image this job builds"
    )


def test_pin_check_rejects_a_tag() -> None:
    assert SHA.fullmatch("v0.36.0") is None
