"""Regression guard for the gh tarball URL composition in deploy/Dockerfile.

SA-07's install step composed the gh download URL from a template that
already contains the ``_linux_`` separator while the ``GH_ARCH`` case
values also carried the ``linux_`` prefix (``GH_ARCH=linux_amd64``), so
every build requested ``gh_2.102.0_linux_linux_amd64.tar.gz`` and died on
a 404 -- in CI and locally, on every master push from 2026-10-30 until the
fix. ``tests/smoke/test_image_tooling_contract.py`` pins the digests, the
versions and the case ordering, but nothing asserted the *composed asset
name*, which is why 25 publish runs could fail while the suite stayed
green (the contract test even passes today on the broken file).

This file pins exactly that composition: the template must stay, the case
values must be bare arches, and each bare arch must be one of the suffix
keys ``image_tooling_pins.json`` declares for the gh release.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "deploy" / "Dockerfile"
PINS = Path(__file__).with_name("image_tooling_pins.json")


def _runtime_code() -> str:
    """The runtime stage with comment-only lines dropped (see the contract test)."""
    lines = DOCKERFILE.read_text(encoding="utf-8").splitlines()
    starts = [
        index
        for index, line in enumerate(lines)
        if re.match(r"^\s*FROM\s+\S+\s+AS\s+runtime\s*$", line, re.IGNORECASE)
    ]
    assert starts, "deploy/Dockerfile must declare a `FROM ... AS runtime` stage"
    stage = "\n".join(lines[starts[-1] :])
    return "\n".join(
        line for line in stage.splitlines() if not line.lstrip().startswith("#")
    )


def test_gh_download_url_composes_the_pinned_asset_name() -> None:
    runtime = _runtime_code()
    pins = json.loads(PINS.read_text(encoding="utf-8"))

    # The template contributes the `_linux_` separator, so the case values
    # must be bare arches: a value that also carries the prefix composes a
    # doubled separator and the build 404s on an asset that does not exist.
    assert "linux_linux" not in runtime, (
        "the gh URL template already contains `_linux_`; a GH_ARCH value "
        "that also carries the `linux_` prefix composes a doubled separator "
        "and the download 404s"
    )
    assert "GH_ARCH=linux_amd64" not in runtime
    assert "GH_ARCH=linux_arm64" not in runtime

    # Each bare arch must be exactly the suffix key the pins file declares
    # for the gh release, so the composed URL names a real upstream asset.
    for suffix in pins["gh"]["sha256"]:  # keys: linux_amd64, linux_arm64
        arch = suffix.removeprefix("linux_")
        assert re.search(rf"^\s*GH_ARCH={arch};", runtime, re.MULTILINE), (
            f"the arch mapping must set GH_ARCH={arch} so the URL composes "
            f"gh_{pins['gh']['version']}_{suffix}.tar.gz"
        )
    assert "gh_${GH_VERSION}_linux_${GH_ARCH}.tar.gz" in runtime
