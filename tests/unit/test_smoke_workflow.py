"""Mechanical guards for .github/workflows/smoke.yml (DK-04).

The smoke workflow is the only job that grades deploy/Dockerfile and
deploy/compose.yaml end to end: a planning container has the docker client but
no daemon, so `docker build` and `docker compose up --wait` are exercised here
first. Two properties of that workflow are mechanical, so they are checked
mechanically:

  * every action is referenced by full 40-character commit SHA. A tag is a
    mutable ref, and a moved tag would silently change what runs in the one job
    that holds `packages: write`;
  * `packages: write` is granted to exactly one job - the job that runs
    docker/build-push-action - so the build-and-smoke job cannot push to GHCR.

PyYAML is not a dependency of this repo (requirements-dev.txt does not list it),
so the workflow is read with a small indentation-aware line scanner instead of
`yaml.safe_load`. Inline comments are removed first: a correct pin is
`uses: actions/checkout@<sha> # v4.2.2`, and matching the raw line would fail on
a correct file.
"""
from __future__ import annotations

import pathlib
import re

WORKFLOW = (
    pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows" / "smoke.yml"
)

#: An action reference pinned to a full 40-character commit SHA.
PINNED = re.compile(r"[^@\s]+@[0-9a-f]{40}")


def _entries():
    """Return (indent, body) per non-blank line, inline comments removed.

    A leading "- " list marker is dropped so a list item and the sibling mapping
    keys around it can be compared by indentation alone.
    """
    assert WORKFLOW.is_file(), f"missing {WORKFLOW}"
    entries = []
    for raw in WORKFLOW.read_text().splitlines():
        line = re.sub(r"\s+#.*$", "", raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()
        if body.startswith("- "):
            body = body[2:].strip()
        entries.append((indent, body))
    return entries


def _key(body):
    """Split a mapping line into (key, value-after-the-colon)."""
    key, _, value = body.partition(":")
    return key.strip(), value.strip()


def _block(entries, start):
    """Indices of the entries nested under entries[start]."""
    indices = []
    index = start + 1
    while index < len(entries) and entries[index][0] > entries[start][0]:
        indices.append(index)
        index += 1
    return indices


def _permissions(entries, index):
    """The permissions value declared at entries[index]: a string or a mapping."""
    _, value = _key(entries[index][1])
    if value:
        return value
    mapping = {}
    for child in _block(entries, index):
        key, nested = _key(entries[child][1])
        mapping[key] = nested
    return mapping


def _jobs():
    """Map job id -> {"permissions": value or None, "uses": [action refs]}."""
    entries = _entries()
    starts = [
        index
        for index, (indent, body) in enumerate(entries)
        if indent == 0 and _key(body)[0] == "jobs"
    ]
    assert starts, "workflow declares no jobs"
    children = _block(entries, starts[0])
    assert children, "workflow declares no jobs"
    job_indent = min(entries[index][0] for index in children)
    jobs = {}
    for index in children:
        indent, body = entries[index]
        if indent != job_indent:
            continue
        members = _block(entries, index)
        permissions = None
        uses = []
        permission_indices = [
            member
            for member in members
            if _key(entries[member][1])[0] == "permissions"
        ]
        if permission_indices:
            shallowest = min(permission_indices, key=lambda member: entries[member][0])
            permissions = _permissions(entries, shallowest)
        for member in members:
            key, value = _key(entries[member][1])
            if key == "uses":
                uses.append(value)
        jobs[_key(body)[0]] = {"permissions": permissions, "uses": uses}
    return jobs


def _grants_packages_write(permissions):
    """True when a permissions value lets the holder push to a registry."""
    if permissions == "write-all":
        return True
    return isinstance(permissions, dict) and permissions.get("packages") == "write"


def test_stack_job_is_advisory_and_publish_is_not_gated_by_it():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "if: vars.ENABLE_SMOKE_CI == 'true'" in text, (
        "the stack job must be gated on ENABLE_SMOKE_CI: the compose stack does "
        "not yet boot on a Linux engine, and a red smoke check on every pull "
        "request would block every merge. Remove this assertion when the stack "
        "boots and the gate comes off."
    )
    assert "if: always() && github.event_name == 'push'" in text, (
        "the publish job must still run when the stack job is skipped, or the "
        "image stops shipping on every push to master"
    )


def test_actions_are_pinned_to_full_sha():
    offenders = []
    for _, body in _entries():
        key, value = _key(body)
        if key == "uses" and not PINNED.fullmatch(value):
            offenders.append(value)
    assert offenders == [], (
        f"every action must be pinned to a full 40-char commit SHA, offenders: {offenders}"
    )


def test_packages_write_only_on_publish_job():
    # Built per call: a module-level list or a mutable default argument would
    # keep violations from an earlier call and fail a later clean workflow.
    violations = []
    entries = _entries()
    top_level = [
        index
        for index, (indent, body) in enumerate(entries)
        if indent == 0 and _key(body)[0] == "permissions"
    ]
    assert top_level, "workflow must declare a top-level permissions block"
    top = _permissions(entries, top_level[0])
    if top == "write-all":
        violations.append("workflow-level write-all")
    if isinstance(top, dict) and top.get("packages") == "write":
        violations.append("workflow-level packages: write")

    jobs = _jobs()
    granting = [
        job_id for job_id, job in jobs.items() if _grants_packages_write(job["permissions"])
    ]
    assert len(granting) == 1, (
        f"exactly one job may hold packages: write, these do: {granting}"
    )
    publish = granting[0]
    assert any(
        ref.startswith("docker/build-push-action@") for ref in jobs[publish]["uses"]
    ), (
        f"the job holding packages: write ({publish}) must be the image publish "
        "job, i.e. run docker/build-push-action"
    )
    others = [
        job_id
        for job_id, job in jobs.items()
        if job_id != publish
        and _grants_packages_write(job["permissions"])
        and job["permissions"] != "write-all"
    ]
    assert others == [], f"only {publish} may hold packages: write, these do: {others}"
    assert violations == [], f"workflow-level permissions must stay read-only: {violations}"
