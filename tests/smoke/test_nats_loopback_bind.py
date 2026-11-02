"""Contract tests: the NATS broker binds its listeners to loopback.

Both compose stacks run the broker in the pipeline container's network
namespace (``network_mode: "service:pipeline"``). That namespace is attached to
the compose project network, so binding ``-a 0.0.0.0`` would expose port 4222
(publish/subscribe/JetStream injection) and the unauthenticated monitoring
endpoint on 8222 to every other service on that network.

The stacks are read as plain text so this suite needs no YAML dependency: the
``nats`` service's ``command`` is a JSON array, which ``json.loads`` parses
exactly.
"""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

STANDALONE_COMPOSE = REPO_ROOT / "deploy" / "standalone" / "compose.yaml"
SHARED_COMPOSE = REPO_ROOT / "deploy" / "compose.yaml"

BARE_PORT = re.compile(r"^[0-9]+$")


def _nats_command(path: Path) -> list[str]:
    """Return the ``command`` array of the ``nats:`` service in *path*."""
    lines = path.read_text(encoding="utf-8").splitlines()

    # Locate the `nats:` service key: a top-level-under-services key indented by
    # exactly two spaces.
    service_start = None
    for index, line in enumerate(lines):
        if line.rstrip() == "  nats:":
            service_start = index
            break
    assert service_start is not None, f"no `nats:` service found in {path}"

    # The service body runs until the next key at the same indent level.
    command = None
    for line in lines[service_start + 1 :]:
        if line.strip() and not line.startswith("   ") and not line.startswith("  #"):
            break
        stripped = line.strip()
        if stripped.startswith("command:"):
            raw = stripped[len("command:") :].strip()
            command = json.loads(raw)
            break
    assert command is not None, f"no `command:` for the nats service in {path}"
    assert isinstance(command, list), (
        f"the nats command in {path} must be a JSON array, got {command!r}"
    )
    return command


@pytest.mark.parametrize(
    "path", [STANDALONE_COMPOSE, SHARED_COMPOSE], ids=["standalone", "shared"]
)
def test_nats_client_listener_binds_loopback(path: Path) -> None:
    command = _nats_command(path)

    assert "-a" in command, f"{path}: nats command sets no listen address"
    listen_address = command[command.index("-a") + 1]
    assert listen_address == "127.0.0.1", (
        f"{path}: nats client listener must bind 127.0.0.1, found {listen_address!r}"
    )
    assert not any("0.0.0.0" in argument for argument in command), (
        f"{path}: nats command must not mention 0.0.0.0 anywhere, found {command!r}"
    )


@pytest.mark.parametrize(
    "path", [STANDALONE_COMPOSE, SHARED_COMPOSE], ids=["standalone", "shared"]
)
def test_nats_monitor_port_is_a_bare_port(path: Path) -> None:
    command = _nats_command(path)

    assert "-m" in command, f"{path}: nats command sets no monitoring port"
    monitor = command[command.index("-m") + 1]
    # nats-server's -m/--http_port accepts a port only; a `host:port` value
    # makes nats-server print its usage and exit, taking the stack down.
    assert BARE_PORT.match(monitor), (
        f"{path}: -m must be a bare port number, found {monitor!r}"
    )


def test_standalone_compose_drops_the_false_loopback_claim() -> None:
    text = STANDALONE_COMPOSE.read_text(encoding="utf-8")
    assert "no other container can reach this broker" not in text, (
        "deploy/standalone/compose.yaml still claims no other container can "
        "reach the broker, which a 0.0.0.0 bind contradicts"
    )


def test_shared_compose_drops_the_false_monitor_bind_claim() -> None:
    text = SHARED_COMPOSE.read_text(encoding="utf-8")
    assert "monitor therefore binds 0.0.0.0" not in text, (
        "deploy/compose.yaml still claims the monitoring endpoint binds 0.0.0.0, "
        "which the loopback listen address contradicts"
    )
