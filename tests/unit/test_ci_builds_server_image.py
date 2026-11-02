"""ci.yml must build the server image on every pull request.

SA-07's doubled ``linux_`` URL broke ``docker build -f deploy/Dockerfile``
everywhere, yet merged clean: PR CI never built the image, the only build
lived in the post-merge publish job (``smoke.yml``), and a post-merge-only
failure gates no merge and notifies nobody -- 25 red publish runs between
2026-10-30 and 2026-11-02 landed nothing and were noticed by no one.

This pins the PR-side build job that closes that blind spot: a change
that breaks the image must fail its own pull request, where the merge
gate can see it.
"""

from __future__ import annotations

from pathlib import Path

CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def test_ci_builds_the_server_image_on_prs() -> None:
    text = CI.read_text(encoding="utf-8")
    assert "docker build -f deploy/Dockerfile" in text, (
        "PR CI must build deploy/Dockerfile; the only build today is the "
        "post-merge publish job in smoke.yml, which fails after the merge "
        "and gates nothing"
    )
