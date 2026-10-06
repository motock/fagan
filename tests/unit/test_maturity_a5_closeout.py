"""Closeout guard for the A5 retro-derived harness improvements (HK-1).

Each of the seven shipped A5 items must be ticked and carry its retro id; the
tier-asymmetry item is deliberately still open (the benchmark plan's
step-budget experiment closes it).
"""

from pathlib import Path

PLAN_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "plans"
    / "MATURITY_AND_UNIQUENESS_PLANS.md"
)

CONTINUATION_INDENT = " " * 6


def _bullet(title: str) -> str:
    """Return the bullet whose first line contains ``title``, with continuations.

    A bullet is the line starting with ``- [`` that contains the title, plus any
    directly following lines indented by six spaces. Continuation lines matter:
    the RH-08 id lands on the second line of its bullet.
    """
    lines = PLAN_PATH.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("- [") and title in line:
            bullet = [line]
            for continuation in lines[index + 1 :]:
                if not continuation.startswith(CONTINUATION_INDENT):
                    break
                bullet.append(continuation)
            return "\n".join(bullet)
    raise AssertionError(f"no bullet found containing {title!r}")


def test_rh_01_anchor_the_rebrief_header_detectors_is_done():
    bullet = _bullet("anchor the rebrief-header detectors")
    assert bullet.startswith("- [x]")
    assert "RH-01" in bullet


def test_rh_02_split_brief_patched_by_cause_is_done():
    bullet = _bullet("split `brief_patched` by cause")
    assert bullet.startswith("- [x]")
    assert "RH-02" in bullet


def test_rh_03_sew5_env_conflict_guard_is_done():
    bullet = _bullet("make the SEW-5 env-conflict guard diff sources, not a catalog")
    assert bullet.startswith("- [x]")
    assert "RH-03" in bullet


def test_rh_04_success_report_repo_filter_is_done():
    bullet = _bullet("give the success report a repo filter")
    assert bullet.startswith("- [x]")
    assert "RH-04" in bullet


def test_rh_05_unfreeze_the_failure_mode_catalog_is_done():
    bullet = _bullet("unfreeze the failure-mode catalog")
    assert bullet.startswith("- [x]")
    assert "RH-05" in bullet


def test_rh_06_bound_the_retro_backlog_is_done():
    bullet = _bullet("bound the retro backlog")
    assert bullet.startswith("- [x]")
    assert "RH-06" in bullet


def test_rh_08_ci_line_count_gate_is_done():
    bullet = _bullet("add a CI line-count gate for the 1000-line rule")
    assert bullet.startswith("- [x]")
    assert "RH-08" in bullet


def test_tier_asymmetry_item_is_still_open():
    bullet = _bullet("resolve the on-device/cloud-oss tier asymmetry deliberately")
    assert bullet.startswith("- [ ]")
