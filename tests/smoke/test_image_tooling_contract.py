"""Static contract for the *tooling* the runtime image must ship (SA-07).

``tests/unit/test_deploy_dockerfile.py`` covers the server image's shape, and
``tests/smoke/test_deploy_contract.py`` covers the smoke stack. Neither answers
"can the image in ``deploy/Dockerfile`` actually operate a code host and talk to
the Claude backend?" -- because until SA-07 the runtime stage had no ``gh`` (so
no pull requests, no merges) and no ``claude`` CLI (so no Claude backend at all).

Those two binaries are also the two easiest things in the image to get silently
wrong, so this file pins the properties that make an install *reproducible*
rather than merely present:

* every download is verified with ``sha256sum -c`` against a pinned digest, so a
  truncated tarball or a re-tagged upstream release fails the build instead of
  poisoning a layer;
* the Claude Code package is pinned to an exact version -- ``^``/``~`` ranges
  would let a rebuild pick up unreviewed code;
* the runtime stage gains no compiler toolchain: the whole point of the
  multi-stage build is that build-time code never reaches the shipped image;
* the image still ends ``USER 10001:10001``, because everything above the install
  step runs as root and a stray ``USER`` after it would ship a root container.

Everything here is read as text: ``docker build`` needs a daemon the unit-test
container does not have, so these run before the image is ever built.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "deploy" / "Dockerfile"

#: sha256 of gh 2.102.0, per platform.
GH_CHECKSUMS = {
    "amd64": "bb766f710eef8ede859c18578c72c327597cd4c8a85b06001b1f3843c6019386",
    "arm64": "7862c86c72f43df3a2d93ddde6f473285b4e2af61b494849846827e513ef6484",
}

#: sha256 of node-v24.22.0-linux-<arch>.tar.xz, per platform.
NODE_CHECKSUMS = {
    "x64": "cc2f601cee82d9a5a233302fe307abc600727035fd7d2c3a3571755989e95008",
    "arm64": "fef00bb4d36e1c50104a9775a10359beee98f162b4104dd12472c75065c6a143",
}

ALL_CHECKSUMS = tuple(GH_CHECKSUMS.values()) + tuple(NODE_CHECKSUMS.values())

#: The exact Claude Code release the image installs.
CLAUDE_CODE_PACKAGE = "@anthropic-ai/claude-code@2.1.301"

#: Instructions that would drag a compiler toolchain into the shipped layer.
TOOLCHAIN_MARKERS = ("build-essential", "gcc", " make")


def _text(path: Path) -> str:
    assert path.exists(), f"missing {path}"
    return path.read_text(encoding="utf-8")


def _runtime_stage(text: str) -> str:
    """Everything from the last ``FROM ... AS runtime`` to end of file.

    Sliced, not searched whole: the builder stage is allowed to do anything it
    likes (that is the point of a multi-stage build), so a bare ``"gcc" in text``
    check would be a false positive waiting to happen. Only the runtime stage is
    what ships.
    """
    lines = text.splitlines()
    starts = [
        index
        for index, line in enumerate(lines)
        if re.match(r"^\s*FROM\s+\S+\s+AS\s+runtime\s*$", line, re.IGNORECASE)
    ]
    assert starts, "deploy/Dockerfile must declare a `FROM ... AS runtime` stage"
    return "\n".join(lines[starts[-1] :])


def _last_user(runtime: str) -> str:
    """The final ``USER`` instruction of the runtime stage, stripped.

    Collected and indexed rather than located once: ``USER root`` appended after
    the real one must fail this test, and ``splitlines()[-1]`` would miss it
    whenever the file ends with a blank line.
    """
    users = [
        line.strip()
        for line in runtime.splitlines()
        if re.match(r"^\s*USER\s+", line)
    ]
    assert users, "the runtime stage must declare a USER"
    return users[-1]


def test_runtime_stage_exists() -> None:
    runtime = _runtime_stage(_text(DOCKERFILE))
    assert runtime, "the runtime stage slice must not be empty"


def _runtime_code(text: str) -> str:
    """The runtime stage with comment-only lines dropped.

    Prose must not satisfy a code assertion: the install step's own explanatory
    comment mentions ``sha256sum -c``, and a naive whole-slice search would
    report verification that a reader had merely *described*. It also keeps the
    toolchain assertion from tripping on a comment that says "no gcc".
    """
    return "\n".join(
        line
        for line in _runtime_stage(text).splitlines()
        if not line.lstrip().startswith("#")
    )


def test_downloads_are_verified_with_pinned_sha256() -> None:
    runtime = _runtime_code(_text(DOCKERFILE))
    missing = [digest for digest in ALL_CHECKSUMS if digest not in runtime]
    assert not missing, f"runtime stage is missing pinned sha256 digests: {missing}"
    checks = re.findall(r"sha256sum\s+-c\b", runtime)
    assert len(checks) >= len(ALL_CHECKSUMS) // 2, (
        "each download must be verified with `sha256sum -c -` (a bare `sha256sum` "
        f"prints a hash, exits 0, and verifies nothing); found {len(checks)}"
    )


def test_claude_code_is_installed_at_an_exact_version() -> None:
    runtime = _runtime_stage(_text(DOCKERFILE))
    assert CLAUDE_CODE_PACKAGE in runtime, (
        f"runtime stage must install {CLAUDE_CODE_PACKAGE}"
    )
    # Scoped to the package spec, not the whole stage: a tilde in some unrelated
    # comment is not a version range, and failing on one would be a landmine for
    # every future edit of this file.
    specs = re.findall(r"@anthropic-ai/claude-code@([^\s\"';\\]+)", runtime)
    assert specs, f"runtime stage must name {CLAUDE_CODE_PACKAGE}"
    ranged = [spec for spec in specs if spec[0] in {"^", "~"}]
    assert not ranged, (
        f"the Claude Code package must be pinned exactly, found ranges: {ranged}"
    )
    assert all(spec == "2.1.301" for spec in specs), (
        f"every Claude Code install must pin 2.1.301, found {specs}"
    )


def test_versions_are_declared_as_build_args() -> None:
    runtime = _runtime_code(_text(DOCKERFILE))
    for declaration in (
        "ARG GH_VERSION=2.102.0",
        "ARG NODE_VERSION=24.22.0",
        "ARG CLAUDE_CODE_VERSION=2.1.301",
        "ARG TARGETARCH",
    ):
        assert declaration in runtime, f"runtime stage must declare `{declaration}`"
    # The args must live inside the stage, after its FROM: declared globally
    # (above the FROM) they are invisible here, `$GH_VERSION` expands to empty
    # and the download URL becomes `gh__linux_amd64.tar.gz`.
    assert runtime.lstrip().upper().startswith("FROM "), (
        "the ARGs must follow the runtime stage's FROM, not precede it"
    )


def test_runtime_stage_has_no_compiler_toolchain() -> None:
    runtime = _runtime_code(_text(DOCKERFILE))
    found = [marker for marker in TOOLCHAIN_MARKERS if marker in runtime]
    assert not found, f"runtime stage must not ship a compiler toolchain: {found}"


def test_image_still_ends_as_the_unprivileged_user() -> None:
    runtime = _runtime_stage(_text(DOCKERFILE))
    assert _last_user(runtime) == "USER 10001:10001", (
        "the last USER instruction must be `USER 10001:10001`; anything after it "
        "runs as uid 10001 and cannot write /usr/local/bin"
    )
