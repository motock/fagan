"""Structural guards for the smoke workflow (`.github/workflows/smoke.yml`).

The workflow runs the deploy stack end to end and publishes the image to ghcr,
so two properties have to hold mechanically or the job is either unpinnable or
over-privileged:

* every action it uses is pinned to a full 40-character commit SHA - a tag or a
  shortened SHA can be moved under the workflow between reviews, and a 7-char
  ref is not the same object a reviewer inspected;
* `packages: write` is granted to exactly one job, and that job is the publish
  job gated to `push` on `master`, so a pull request from any source never gets
  a credential that can push to the registry.

PyYAML is not a dependency of this project (neither the venv nor the runtime
requirements carry it), so the file is read as text. The scan below is
indentation-aware, which keeps it honest against two traps a naive line grep
falls for: a commented-out ``# uses: foo@v3`` line is not a live step, and a
`uses:`-looking line inside a `run: |` block is shell text, not a step key.
"""

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "smoke.yml"

SHA_RE = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
BLOCK_SCALAR_RE = re.compile(r":\s*[|>][-+]?\s*$")


def _raw_lines() -> list[str]:
    """The workflow's lines, or an assertion failure naming the missing path."""
    assert WORKFLOW.is_file(), f"missing workflow: {WORKFLOW}"
    return WORKFLOW.read_text(encoding="utf-8").splitlines()


def _live() -> list[tuple[int, str]]:
    """Return `(indent, text)` for every live line of the workflow.

    Comments and blank lines are dropped, a leading `- ` list marker is folded
    into the indentation so a list item's keys read like their neighbours, and
    the body of a block scalar (`run: |`, `run: >-`) is skipped entirely: those
    lines are shell, not YAML, and must not be scanned for keys.
    """
    live: list[tuple[int, str]] = []
    block_indent: int | None = None
    for raw in _raw_lines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if block_indent is not None:
            if indent > block_indent:
                continue
            block_indent = None
        if stripped.startswith("- "):
            stripped = stripped[2:]
            indent += 2
        text = re.sub(r"\s+#.*$", "", stripped).rstrip()
        live.append((indent, text))
        if BLOCK_SCALAR_RE.search(text):
            block_indent = indent
    return live


def _code() -> list[str]:
    """Every non-comment line, block-scalar bodies included.

    Used for content that lives inside a block scalar (a multi-line `tags: |`
    is values, not keys) while still ignoring commented-out examples.
    """
    return [raw.strip() for raw in _raw_lines() if raw.strip() and not raw.strip().startswith("#")]


def _body(entries: list[tuple[int, str]], key: str, indent: int) -> list[tuple[int, str]]:
    """Return the entries nested under `key:` declared at `indent`."""
    for index, (entry_indent, text) in enumerate(entries):
        if entry_indent != indent:
            continue
        if text.split(":", 1)[0].strip() != key:
            continue
        body: list[tuple[int, str]] = []
        for child_indent, child_text in entries[index + 1 :]:
            if child_indent <= indent:
                break
            body.append((child_indent, child_text))
        return body
    return []


def _job_names(entries: list[tuple[int, str]]) -> list[str]:
    return [text[:-1] for entry_indent, text in _body(entries, "jobs", 0) if entry_indent == 2 and text.endswith(":")]


def _job(entries: list[tuple[int, str]], name: str) -> list[tuple[int, str]]:
    return _body(_body(entries, "jobs", 0), name, 2)


def _values(entries: list[tuple[int, str]], key: str, indent: int) -> list[str]:
    return [text.split(":", 1)[1].strip() for entry_indent, text in entries if entry_indent == indent and text.startswith(f"{key}:")]


def _index_of(entries: list[tuple[int, str]], needle: str) -> int:
    for index, (_indent, text) in enumerate(entries):
        if needle in text:
            return index
    return -1


def test_all_actions_pinned_to_full_sha() -> None:
    """Every `uses:` is a 40-character commit SHA, not a tag or short ref."""
    uses = [text.split(":", 1)[1].strip() for _indent, text in _live() if text.startswith("uses:")]
    assert len(uses) >= 5, f"expected at least 5 uses:, found {uses}"
    for ref in uses:
        assert SHA_RE.match(ref), f"not pinned to a 40-char commit SHA: {ref}"


def test_packages_write_only_on_publish_job() -> None:
    """`packages: write` lives on one job only, and that job pushes to master."""
    entries = _live()
    for _indent, text in _body(entries, "permissions", 0):
        assert text != "packages: write", "workflow-level permissions must not grant packages: write"

    holders = [name for name in _job_names(entries) if "packages: write" in [text for _i, text in _job(entries, name)]]
    assert len(holders) == 1, f"packages: write must be granted to exactly one job, got {holders}"
    gates = _values(_job(entries, holders[0]), "if", 4)
    assert gates, f"the packages: write job has no if: gate: {holders[0]}"
    assert "refs/heads/master" in gates[0], f"the packages: write job must be gated to push on master, got {gates[0]!r}"


def test_smoke_job_captures_logs_and_tears_down_after_failure() -> None:
    """The red path still produces logs and still removes the containers.

    Capture, upload and teardown each need `if: always()`: a failing smoke
    assertion otherwise skips every later step, so the artifact never exists
    and the stack leaks. Teardown has to come last - `down -v` deletes the
    containers, so a `logs` call after it prints nothing.
    """
    entries = _live()
    steps = _job(entries, "build")
    start = _index_of(steps, "up -d --wait")
    smoke = _index_of(steps, "deploy/smoke/smoke.sh")
    capture = _index_of(steps, "logs --no-color")
    upload = _index_of(steps, "upload-artifact")
    teardown = _index_of(steps, "down -v")
    assert -1 not in (start, smoke, capture, upload, teardown), (
        f"missing a smoke step: up={start} smoke={smoke} capture={capture} upload={upload} teardown={teardown}"
    )
    assert start < smoke < capture < upload < teardown, "steps must run up -> smoke -> capture -> upload -> teardown"

    always = [index for index, (_indent, text) in enumerate(steps) if text == "if: always()"]
    assert len(always) >= 3, f"capture, upload and teardown all need if: always(), got {len(always)}"
    for index in (capture, upload, teardown):
        assert any(0 < index - marker <= 2 for marker in always), f"step at index {index} is not guarded by if: always()"

    assert "continue-on-error: true" not in [text for _indent, text in entries], (
        "continue-on-error would report the job green while the smoke assertions failed"
    )


def test_publish_tags_immutable_sha_and_master() -> None:
    """Publishing keeps a rollback point and moves the master tag forward."""
    entries = _live()
    publish = _job(entries, "publish")
    assert _values(publish, "if", 4), "the publish job must be gated"
    assert _values(publish, "needs", 4) == ["build"], "publish must need the smoke job so a red run cannot publish"
    code = "\n".join(_code())
    assert "${{ env.IMAGE }}:${{ github.sha }}" in code, "publish must emit an immutable per-commit tag"
    assert "${{ env.IMAGE }}:master" in code, "publish must move the master tag"
    assert _values(publish, "sbom", 10) == ["true"], "publish must attach an SBOM"
    assert _values(publish, "provenance", 10) == ["mode=max"], "publish must attach provenance attestations"
    text = "\n".join(text for _indent, text in entries)
    assert ":latest" not in text, "a mutable latest tag defeats the per-commit tag"
    assert "pull_request.head.sha" not in text, "head.sha is empty on a push event, which yields an invalid tag"
