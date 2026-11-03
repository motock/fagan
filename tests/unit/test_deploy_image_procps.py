import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "deploy" / "Dockerfile"


def _split_stages(text: str) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    last_from = None
    for index, line in enumerate(lines):
        if re.match(r"^FROM ", line):
            last_from = index
    assert last_from is not None, "Dockerfile has no FROM line"
    return "".join(lines[:last_from]), "".join(lines[last_from:])


def _assert_runtime_installs_procps(text: str) -> None:
    builder, runtime = _split_stages(text)
    if re.search(r"\bprocps\b", builder):
        raise AssertionError("procps must not be in the builder stage")
    if not re.search(r"apt-get install[^\n]*\bprocps\b", runtime):
        raise AssertionError("runtime stage must install procps")


def test_runtime_stage_installs_procps() -> None:
    _assert_runtime_installs_procps(DOCKERFILE.read_text())


def test_rejects_a_runtime_stage_without_procps() -> None:
    text = DOCKERFILE.read_text().replace("xz-utils procps", "xz-utils", 1)
    try:
        _assert_runtime_installs_procps(text)
    except AssertionError:
        return
    raise AssertionError("expected AssertionError when procps is missing from runtime apt line")


def test_rejects_procps_in_the_builder_stage() -> None:
    text = DOCKERFILE.read_text()
    builder, runtime = _split_stages(text)
    bad_text = builder + "RUN apt-get install -y procps\n" + runtime
    try:
        _assert_runtime_installs_procps(bad_text)
    except AssertionError:
        return
    raise AssertionError("expected AssertionError when builder stage installs procps")
