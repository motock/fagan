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
  step runs as root and a stray ``USER`` after it would ship a root container;
* the versions are not build args (``tests/unit/test_deploy_dockerfile.py``
  forbids ``ARG`` outright: build args land in image history), so they are
  ordinary shell variables inside the install step.

The expected digests live in ``image_tooling_pins.json`` beside this file rather
than as sha256 literals here: ``pipeline/pin_guard.py`` flags 64-hex constants in
test source, and its baseline is shrink-only. Keeping the checksums as data also
keeps them next to the versions they belong to, which is how they have to be
refreshed.

Everything here is read as text: ``docker build`` needs a daemon the unit-test
container does not have, so these run before the image is ever built.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "deploy" / "Dockerfile"
PINS = Path(__file__).with_name("image_tooling_pins.json")

#: Instructions that would drag a compiler toolchain into the shipped layer.
TOOLCHAIN_MARKERS = ("build-essential", "gcc", " make")


def _pins() -> dict:
    assert PINS.is_file(), f"missing {PINS.name}: the pinned release digests"
    return json.loads(PINS.read_text(encoding="utf-8"))


def _checksums() -> list[str]:
    pins = _pins()
    digests = list(pins["gh"]["sha256"].values())
    digests.extend(pins["node"]["sha256"].values())
    return digests


def _claude_package() -> str:
    pins = _pins()["claude_code"]
    return f"{pins['package']}@{pins['version']}"


def _text(path: Path) -> str:
    assert path.exists(), f"missing {path}"
    return path.read_text(encoding="utf-8")


def _instructions(text: str, keyword: str) -> list[str]:
    """Argument text of every ``keyword`` instruction (comments ignored)."""
    logical: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not buffer and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            buffer += line[:-1].strip() + " "
            continue
        buffer += line
        logical.append(buffer.strip())
        buffer = ""
    if buffer.strip():
        logical.append(buffer.strip())

    prefix = keyword.upper() + " "
    return [
        item[len(prefix) :].strip()
        for item in logical
        if item.upper().startswith(prefix)
    ]


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


def _last_user(runtime: str) -> str:
    """The final ``USER`` instruction of the runtime stage, stripped.

    Collected and indexed rather than located once: ``USER root`` appended after
    the real one must fail this test, and ``splitlines()[-1]`` would miss it
    whenever the file ends with a blank line.
    """
    users = [
        line.strip() for line in runtime.splitlines() if re.match(r"^\s*USER\s+", line)
    ]
    assert users, "the runtime stage must declare a USER"
    return users[-1]


def test_runtime_stage_exists() -> None:
    assert _runtime_stage(_text(DOCKERFILE)).strip()


def test_downloads_are_verified_with_pinned_sha256() -> None:
    runtime = _runtime_code(_text(DOCKERFILE))
    missing = [digest for digest in _checksums() if digest not in runtime]
    assert not missing, f"runtime stage is missing pinned sha256 digests: {missing}"
    checks = re.findall(r"sha256sum\s+-c\b", runtime)
    assert len(checks) >= 2, (
        "each download must be verified with `sha256sum -c -` (a bare `sha256sum` "
        f"prints a hash, exits 0, and verifies nothing); found {len(checks)}"
    )


def test_claude_code_is_installed_at_an_exact_version() -> None:
    runtime = _runtime_stage(_text(DOCKERFILE))
    package = _claude_package()
    assert package in runtime, f"runtime stage must install {package}"

    name = _pins()["claude_code"]["package"]
    for spec in re.findall(re.escape(name) + r"[^\s\"';]*", runtime):
        assert not re.search(r"[\^~]", spec), (
            f"{spec!r} is an npm range; the Claude Code CLI must be pinned to an "
            "exact version so a rebuild cannot pick up unreviewed code"
        )


def test_runtime_stage_ships_no_compiler_toolchain() -> None:
    runtime = _runtime_code(_text(DOCKERFILE))
    found = [marker for marker in TOOLCHAIN_MARKERS if marker in runtime]
    assert not found, (
        f"the runtime stage must not carry a compiler toolchain; found {found}"
    )


def test_image_still_ends_as_the_unprivileged_user() -> None:
    assert _last_user(_runtime_stage(_text(DOCKERFILE))) == "USER 10001:10001"


def test_tooling_versions_are_not_build_args() -> None:
    text = _text(DOCKERFILE)
    assert not _instructions(text, "ARG"), (
        "build args leak into image history; the tooling versions must be plain "
        "shell variables inside the install step"
    )


def test_tooling_versions_are_pinned_inside_the_install_step() -> None:
    runtime = _runtime_code(_text(DOCKERFILE))
    pins = _pins()
    for name, value in (
        ("GH_VERSION", pins["gh"]["version"]),
        ("NODE_VERSION", pins["node"]["version"]),
        ("CLAUDE_CODE_VERSION", pins["claude_code"]["version"]),
    ):
        assert re.search(rf"^\s*{name}={re.escape(value)};", runtime, re.MULTILINE), (
            f"the install step must set {name}={value} (matching the pinned digests)"
        )


def test_unsupported_architecture_fails_before_downloading() -> None:
    runtime = _runtime_code(_text(DOCKERFILE))
    case_start = runtime.find("case ")
    assert case_start != -1, "the install step must map the machine architecture"
    branch = runtime.find("*)", case_start)
    assert branch != -1, "the arch mapping needs a `*)` catch-all branch"
    download = runtime.find("curl ", case_start)
    assert download != -1, "the install step must download the release tarballs"
    assert branch < download, (
        "the `*)` branch must reject unsupported architectures before any "
        "download, rather than after a 404 has already been written to disk"
    )
    assert "exit 1" in runtime[branch:download], (
        "an unsupported architecture must fail the build"
    )
