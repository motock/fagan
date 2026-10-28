"""Static contract for the deployable server image (``deploy/Dockerfile``).

The repo-root ``Dockerfile`` packages the MCP server for MCP-directory
listings (single stdio process, no dashboard).  ``deploy/Dockerfile`` is a
different artifact: a long-running server image that runs the dashboard and
the remote MCP transport together, under a non-root account, with all mutable
state on a declared volume.

``docker build`` itself is exercised by the smoke pipeline job, not here.
These tests read the Dockerfile and its entrypoint as text and assert the
invariants that must hold for *every* revision of the image:

* multi-stage build on an exact ``python:3.12-slim`` tag, never ``latest``;
* ``git`` present in the runtime stage (the pipeline shells out to it);
* the app installed from ``requirements.txt``;
* a numeric non-root ``USER`` that applies to the entrypoint;
* ``PLAN_DIR`` / worktree root pointing inside a declared ``VOLUME``;
* an entrypoint that starts the dashboard and the remote MCP transport;
* a ``HEALTHCHECK`` on a credential-free dashboard route (not ``/api/health``,
  which the image's OIDC gate requires a bearer token for);
* no ``.env`` or secret material copied into the image.

They fail on assertion (not on import) before the image exists.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO_ROOT / "deploy"
DOCKERFILE = DEPLOY_DIR / "Dockerfile"
ENTRYPOINT = DEPLOY_DIR / "entrypoint.sh"

BASE_IMAGE = "python:3.12-slim"

# Paths and filename patterns that carry secrets or operator-local state.
# None of them may be copied into an image layer: a COPY bakes the content
# into the image, where it survives in the layer history for anyone who can
# pull it.
SECRET_PATH_PATTERNS = (
    r"(^|/)\.env($|\.)",
    r"(^|/)\.dashboard\.env",
    r"(^|/)\.pipeline\.env",
    r"\.pem$",
    r"\.key$",
    r"id_rsa",
    r"id_ed25519",
    r"\.p12$",
    r"secret",
    r"credential",
)


def _read(path: Path) -> str:
    assert path.is_file(), f"{path.relative_to(REPO_ROOT)} does not exist yet"
    return path.read_text(encoding="utf-8")


def _instructions(text: str, keyword: str) -> list[str]:
    """Return the argument text of every ``keyword`` instruction.

    Handles both single-line and backslash-continued instructions and ignores
    comment lines, so a multi-line ``RUN`` or ``HEALTHCHECK`` is compared as
    one instruction.
    """
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


def _dockerfile() -> str:
    return _read(DOCKERFILE)


def test_dockerfile_is_multi_stage_on_pinned_base() -> None:
    stages = _instructions(_dockerfile(), "FROM")
    assert len(stages) >= 2, f"expected a multi-stage build, found FROM lines: {stages}"

    for stage in stages:
        image = stage.split()[0]
        assert image == BASE_IMAGE, (
            f"every stage must build on the exact tag {BASE_IMAGE!r}, got {image!r}"
        )
        assert "latest" not in stage.lower(), f"stage {stage!r} pins a moving tag"


def test_dockerfile_installs_git_and_app_requirements() -> None:
    text = _dockerfile()
    runs = " ".join(_instructions(text, "RUN"))
    assert re.search(r"\bapt-get install\b[^\n]*\bgit\b", runs), (
        "the runtime stage must install git: the pipeline shells out to it"
    )
    assert re.search(r"requirements\.txt", runs), (
        "the app must be installed from requirements.txt"
    )
    assert re.search(r"pip install[^\n]*requirements\.txt", runs), (
        "requirements.txt must be installed with pip"
    )


def test_dockerfile_runs_as_numeric_non_root_user() -> None:
    users = _instructions(_dockerfile(), "USER")
    assert users, "deploy/Dockerfile must switch to a non-root USER"
    final = users[-1]
    assert re.match(r"^\d+(:\d+)?$", final), (
        f"the final USER must be a numeric uid (optionally uid:gid), got {final!r}"
    )
    assert int(final.split(":")[0]) != 0, "the image must not run as root"

    # The USER must come before the entrypoint, otherwise the service starts
    # as root and drops nothing.
    lines = [
        line.strip()
        for line in _dockerfile().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    last_user = max(i for i, line in enumerate(lines) if line.upper().startswith("USER "))
    entry = [i for i, line in enumerate(lines) if line.upper().startswith("ENTRYPOINT ")]
    assert entry, "deploy/Dockerfile must declare an ENTRYPOINT"
    assert last_user < entry[-1], "USER must be set before ENTRYPOINT"


def test_plan_dir_and_worktree_root_live_on_a_declared_volume() -> None:
    text = _dockerfile()
    env = " ".join(_instructions(text, "ENV"))
    volumes = _instructions(text, "VOLUME")
    assert volumes, "deploy/Dockerfile must declare a VOLUME for mutable state"

    volume_text = " ".join(volumes)
    assert "PLAN_DIR=" in env, "PLAN_DIR must be set so plans persist on the volume"
    assert "WORKTREE_ROOT=" in env, (
        "WORKTREE_ROOT (the name pipeline/paths.py reads) must be set so "
        "worktrees persist on the volume"
    )
    declared = [v.strip('"').rstrip("/") for v in volume_text.replace("[", " ").replace("]", " ").split()]
    assert declared, f"no volume path could be parsed from {volume_text!r}"
    for key in ("PLAN_DIR", "WORKTREE_ROOT"):
        match = re.search(rf"{key}=(\S+)", env)
        assert match, f"{key} must have a literal value in the Dockerfile"
        value = match.group(1).strip('"').rstrip("/")
        assert any(
            value == volume or value.startswith(volume + "/") for volume in declared
        ), f"{key}={value} must live inside a declared VOLUME ({volume_text})"


def test_healthcheck_probes_a_credential_free_dashboard_route() -> None:
    """The probe runs inside the container with no credential available.

    The image selects the OIDC identity provider for the whole process, so
    ``app/dashboard.py``'s middleware requires a bearer token on every
    ``/api/`` route -- ``/api/health`` included. A probe that fetched
    ``/api/health`` anonymously would get a 401 and mark the container
    unhealthy forever. The probe must hit the dashboard on a path outside the
    gated ``/api/`` prefix, which still exercises the app and its API-key
    store.
    """
    healthchecks = _instructions(_dockerfile(), "HEALTHCHECK")
    assert healthchecks, "deploy/Dockerfile must declare a HEALTHCHECK"
    probe = healthchecks[-1]
    assert "/api/health" not in probe, (
        "the HEALTHCHECK must not probe /api/health: the image sets "
        "PIPELINE_IDENTITY_PROVIDER=oidc, so every /api/ route needs a bearer "
        "token and an anonymous probe can only ever get 401"
    )
    assert re.search(r"127\.0\.0\.1:%s/['\"]", probe), (
        f"the HEALTHCHECK must GET the dashboard's index route; got {probe!r}"
    )
    assert "status==200" in probe.replace(" ", ""), (
        "the probe must treat a non-200 response as unhealthy"
    )


def test_workdir_is_writable_by_the_runtime_user() -> None:
    """The dashboard writes its shared-secret key into the working directory.

    ``app/auth.py`` keeps the key at ``REPO_ROOT / ".dashboard_api_key"`` and
    creates it lazily on first use, so the image's WORKDIR must be owned by
    the runtime uid. A root-owned ``/app`` makes the index route raise
    ``PermissionError`` and answer 500 -- which is also the route the
    HEALTHCHECK exercises.
    """
    text = _dockerfile()
    # The last WORKDIR belongs to the runtime stage; the builder stage's
    # scratch directory never holds application state.
    workdirs = _instructions(text, "WORKDIR")
    assert workdirs, "deploy/Dockerfile must set a WORKDIR"
    workdir = workdirs[-1]

    users = _instructions(text, "USER")
    assert users, "deploy/Dockerfile must switch to a non-root USER"
    uid = int(users[-1].split(":")[0])

    runs = " ".join(_instructions(text, "RUN"))
    chown = re.search(rf"chown\s+(?:-R\s+)?(\d+):\d+\s+{re.escape(workdir)}\b", runs)
    assert chown, (
        f"WORKDIR {workdir!r} is created root-owned and never chowned to the "
        "runtime uid, so the dashboard cannot create .dashboard_api_key there"
    )
    assert int(chown.group(1)) == uid, (
        f"{workdir!r} must be owned by the runtime uid {uid}, got {chown.group(1)}"
    )


def test_entrypoint_starts_dashboard_and_remote_mcp_transport() -> None:
    entry = _entrypoint()
    assert "app.dashboard:app" in entry or "dashboard.py" in entry, (
        "the entrypoint must start the dashboard the same way scripts/dashboard.sh does"
    )
    assert "uvicorn" in entry, "the dashboard is served by uvicorn (see scripts/dashboard.sh)"
    assert "streamable-http" in entry, (
        "the entrypoint must start the remote MCP transport (streamable-http)"
    )
    assert "PIPELINE_MCP_TRANSPORT" in entry, (
        "the transport is selected by PIPELINE_MCP_TRANSPORT"
    )
    assert "PIPELINE_IDENTITY_PROVIDER" in entry and "oidc" in entry, (
        "streamable-http refuses to start without PIPELINE_IDENTITY_PROVIDER=oidc"
    )
    # The secret is mounted, never baked: the entrypoint may only point at a
    # file path.
    assert re.search(r"(_FILE|/run/secrets)", entry), (
        "the OIDC client secret must be read from a mounted file path"
    )
    assert "set -eu" in entry or "set -euo pipefail" in entry, (
        "the entrypoint must fail fast"
    )


def test_no_env_or_secret_path_is_copied_into_the_image() -> None:
    text = _dockerfile()
    copied: list[str] = []
    for instruction in _instructions(text, "COPY") + _instructions(text, "ADD"):
        copied.extend(part for part in instruction.split() if not part.startswith("--"))

    for path in copied:
        lowered = path.lower()
        for pattern in SECRET_PATH_PATTERNS:
            assert not re.search(pattern, lowered), (
                f"{path!r} may carry secrets or operator-local state; "
                "mount it at runtime instead of COPYing it into a layer"
            )

    assert not _instructions(text, "ARG"), (
        "build args leak into image history; secrets must arrive as mounted files"
    )


def test_dockerfile_ignores_nothing_needed_at_runtime() -> None:
    # The build context is the repo root, so the image must not be built from
    # a context that silently omits the entrypoint.
    text = _dockerfile()
    copies = " ".join(_instructions(text, "COPY"))
    assert "deploy/entrypoint.sh" in copies or re.search(r"COPY \. \.", copies), (
        "the entrypoint script must be present in the image"
    )


def _entrypoint() -> str:
    return _read(ENTRYPOINT)
