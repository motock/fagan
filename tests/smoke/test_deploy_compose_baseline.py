"""Regression test for the born-broken ``deploy/compose.yaml`` baseline.

``nats`` shares the pipeline container's network namespace
(``network_mode: "service:pipeline"``), so the *only* possible ordering edge
between the pair is ``nats -> pipeline``. A ``pipeline -> nats`` entry in
``pipeline.depends_on`` closes a two-node cycle and ``docker compose`` refuses
to load the file at all::

    $ docker compose -f deploy/compose.yaml config
    dependency cycle detected: nats -> pipeline -> nats

That made every command in ``deploy/README.md`` (including
``docker compose -f deploy/compose.yaml up -d --wait``) fail before a single
container was created, so the stack could not be started at a clean baseline.

This test asks the real compose CLI to resolve the real file --- no YAML text
scraping, no third-party parser --- and asserts the direction of the surviving
edge. ``docker compose config`` is a purely client-side operation (it renders
the model and validates it; it never contacts the daemon), which is why this
test can run in the unit-test container that has no docker daemon.

The stack refuses to start without its documented throwaway values
(``${VAR:?...}``); the test supplies placeholders, exactly as
``deploy/README.md`` tells an operator to. Nothing here asserts a secret.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose.yaml"

# `docker compose config` is client-side: it renders and validates the model
# without contacting the daemon, so this suite runs in the unit-test container.
# It does need the `docker` CLI itself, which the GitHub macOS runner does not
# ship. Skipping there keeps the assertion (not weakened anywhere) on the legs
# that can make it -- the same shape as the repo's `openssl` / `grep` / `node`
# guards.
_HAS_DOCKER = shutil.which("docker") is not None
pytestmark = pytest.mark.skipif(
    not _HAS_DOCKER, reason="docker CLI not installed; compose config cannot run"
)

# Every `${VAR:?}` in deploy/compose.yaml, with the throwaway value an operator
# is documented to pass. `config` interpolates the file, so these must exist or
# the run fails for an unrelated reason (a missing variable, not a cycle).
_THROWAWAY_ENV = {
    "KEYCLOAK_ADMIN_PASSWORD": "regression-test-placeholder",
    "PIPELINE_DASHBOARD_CLIENT_SECRET": "regression-test-placeholder",
    "PIPELINE_MCP_CLIENT_SECRET": "regression-test-placeholder",
    "PIPELINE_SMOKE_OWNER_PASSWORD": "regression-test-placeholder",
    "PIPELINE_SMOKE_STRANGER_PASSWORD": "regression-test-placeholder",
    "PIPELINE_SMOKE_USER_CLIENT_SECRET": "regression-test-placeholder",
}


def _compose_config() -> dict:
    """Return `docker compose config --format json` for deploy/compose.yaml."""
    docker = shutil.which("docker")
    assert docker, (
        "the docker CLI is required to resolve deploy/compose.yaml; "
        "docker compose config is client-side and needs no daemon"
    )

    # Compose interpolation also refuses a blank `registry` / secret-file
    # variable, so give the environment a deterministic, minimal shape.
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
    env.update(_THROWAWAY_ENV)
    proc = subprocess.run(
        [docker, "compose", "-f", str(COMPOSE_FILE), "config", "--format", "json"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, (
        "`docker compose -f deploy/compose.yaml config` must succeed at the "
        f"repository baseline; exit={proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def compose_config() -> dict:
    return _compose_config()


def test_pipeline_does_not_depend_on_nats(compose_config: dict) -> None:
    """`pipeline -> nats` plus `nats -> pipeline` is an unloadable cycle.

    `nats` joins the pipeline container's network namespace, so it can only
    start once `pipeline` is up: the reverse edge cannot exist.
    """
    pipeline_depends_on = compose_config["services"]["pipeline"].get("depends_on", {})
    assert "nats" not in pipeline_depends_on, (
        "deploy/compose.yaml: services.pipeline.depends_on must NOT contain "
        "'nats' -- nats shares the pipeline container's network namespace "
        "(network_mode: service:pipeline) and already depends_on pipeline, so "
        "this edge is a dependency cycle ('dependency cycle detected: "
        f"nats -> pipeline -> nats'); got pipeline depends_on="
        f"{sorted(pipeline_depends_on)}"
    )


def test_nats_still_depends_on_pipeline(compose_config: dict) -> None:
    """The surviving edge: the broker starts after (inside) the pipeline."""
    nats_depends_on = compose_config["services"]["nats"].get("depends_on", {})
    assert "pipeline" in nats_depends_on, (
        "deploy/compose.yaml: services.nats.depends_on must still contain "
        "'pipeline' -- nats runs in the pipeline container's network "
        f"namespace; got nats depends_on={sorted(nats_depends_on)}"
    )
