"""Broker-backed integration tests for :class:`pipeline.events_nats.NatsEventBus`.

A real ``nats-server -js`` is started on a free loopback port for the session.
When the binary is absent the module **skips** -- except under
``PIPELINE_REQUIRE_NATS_TESTS=1`` (CI exports it), where a missing binary is a
failure: a silently skipped broker suite is exactly how a broken publish path
reaches master.

These tests own the parts of the contract that only a live JetStream can
answer: that the bus really connects, that the managed stream is created with
the documented shape, that the publish subject is ``pipeline.<type>.<plan>``,
and that a repeated ``Nats-Msg-Id`` is deduplicated *by the stream*.

Every case uses its own freshly named stream, so message counts are local to
the case and no case can make another pass or fail by ordering.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

# pipeline.server is imported here, before the pipeline submodules this file
# uses, to defuse a pre-existing import cycle; see the same idiom in
# tests/unit/test_triage_cap_silent_skip.py.
import pipeline.server  # noqa: F401  # isort: skip
from pipeline.events_nats import NatsEventBus

NATS_SERVER = shutil.which("nats-server")
#: Whether a missing binary must fail instead of skip. ``tests/conftest.py``
#: deletes every ``PIPELINE_*`` variable before any test module is imported
#: (hermetic-env scrub), so the CI flag is read here as well as the ambient
#: ``GITHUB_ACTIONS`` marker that the same scrub leaves alone. CI sets both.
REQUIRE_NATS = (
    os.environ.get("PIPELINE_REQUIRE_NATS_TESTS") == "1"
    or os.environ.get("GITHUB_ACTIONS") == "true"
)

pytestmark = pytest.mark.skipif(
    NATS_SERVER is None and not REQUIRE_NATS,
    reason="nats-server not installed",
)

PLAN = "nats-integration-plan"
EVENT_TYPE = "agent_dispatched"
STREAM = "PIPELINE_IT"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"nats-server did not listen on 127.0.0.1:{port}")


async def _jetstream_ready(url: str, timeout: float = 20.0) -> None:
    """Wait until the server answers JetStream API calls.

    The TCP port opens before JetStream finishes initialising, so a bus
    constructed the instant the port is up can see ``ServiceUnavailableError``
    from ``add_stream``.
    """
    import nats

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        nc = None
        try:
            nc = await nats.connect(servers=[url])
            await nc.jetstream().account_info()
            return
        except Exception:  # noqa: BLE001 - any client error means "not ready yet"
            await asyncio.sleep(0.1)
        finally:
            if nc is not None:
                await nc.close()
    raise RuntimeError("nats-server JetStream API did not become available")


@pytest.fixture(scope="session")
def nats_url():
    """URL of a session-scoped ``nats-server -js``, or a loud failure in CI."""
    if NATS_SERVER is None:
        pytest.fail(
            "PIPELINE_REQUIRE_NATS_TESTS=1 but no nats-server binary is on PATH: "
            "the broker suite must not be skipped in CI"
        )

    port = _free_port()
    # A private store per session: the default store directory is shared across
    # runs, and a leftover stream over pipeline.> would make add_stream fail
    # with "subjects overlap with an existing stream".
    store = tempfile.mkdtemp(prefix="pipeline-nats-js-")
    process = subprocess.Popen(
        [NATS_SERVER, "-js", "-a", "127.0.0.1", "-p", str(port), "-sd", store],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    url = f"nats://127.0.0.1:{port}"
    try:
        try:
            _wait_for_port(port)
            asyncio.run(_jetstream_ready(url))
        except (RuntimeError, OSError):
            process.terminate()
            output = process.stdout.read() if process.stdout is not None else b""
            pytest.fail(f"nats-server failed to start: {output[-400:]!r}")
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn server
            process.kill()


@pytest.fixture
def nats_env(monkeypatch, nats_url):
    """Point the bus at the test server.

    One stream per *server*, not per case: a second stream over
    ``pipeline.>`` would overlap the first and the server rejects it, so the
    cases share the session's stream and assert deltas rather than absolutes.
    """
    for name in (
        "PIPELINE_NATS_CREDS_FILE",
        "PIPELINE_NATS_TLS_CA_FILE",
        "PIPELINE_NATS_DURABLE_TYPES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PIPELINE_NATS_URL", nats_url)
    monkeypatch.setenv("PIPELINE_NATS_STREAM", STREAM)
    monkeypatch.setenv("PIPELINE_NATS_MANAGE_STREAM", "1")
    monkeypatch.setenv("PIPELINE_EVENT_PUBLISH_TIMEOUT_S", "5")
    return {"url": nats_url, "stream": STREAM}


@pytest.fixture
def bus(nats_env):
    bus = NatsEventBus()
    try:
        yield bus
    finally:
        bus.close()


def _event(**overrides):
    event = {
        "type": EVENT_TYPE,
        "plan": PLAN,
        "story_key": "BR-03",
        "payload": {"note": "integration"},
    }
    event.update(overrides)
    return event


async def _stream_info(url: str, stream: str):
    """The stream's server-side info."""
    import nats

    nc = await nats.connect(servers=[url])
    try:
        js = nc.jetstream()
        return await js.stream_info(stream)
    finally:
        await nc.close()


async def _stream_messages(url: str, stream: str) -> int:
    """How many messages the stream currently holds."""
    info = await _stream_info(url, stream)
    return info.state.messages


def test_stream_is_created_with_the_documented_shape(bus, nats_env):
    info = asyncio.run(_stream_info(nats_env["url"], nats_env["stream"]))

    assert list(info.config.subjects) == ["pipeline.>"]
    assert info.config.storage == "file"
    assert int(info.config.max_age) == 7 * 24 * 60 * 60


def test_publish_lands_on_the_plan_scoped_subject(bus, nats_env):
    async def receive_one():
        import nats

        nc = await nats.connect(servers=[nats_env["url"]])
        try:
            subject = f"pipeline.{EVENT_TYPE}.{PLAN}"
            subscription = await nc.subscribe(subject)
            await nc.flush()

            await asyncio.to_thread(bus.publish, _event(event_id="e-subject-1"))

            message = await subscription.next_msg(timeout=5)
            return subject, message
        finally:
            await nc.close()

    subject, message = asyncio.run(receive_one())

    assert subject == f"pipeline.{EVENT_TYPE}.{PLAN}"
    assert json.loads(message.data)["event_id"] == "e-subject-1"
    assert message.headers["Nats-Msg-Id"] == "e-subject-1"


def test_duplicate_publish_is_deduplicated_by_the_stream(bus, nats_env):
    before = asyncio.run(_stream_messages(nats_env["url"], nats_env["stream"]))

    first = _event(event_id="e-dup-1", idempotency_key="k-dup-1")
    bus.publish(first)
    bus.publish(dict(first))

    after_duplicate = asyncio.run(
        _stream_messages(nats_env["url"], nats_env["stream"])
    )
    assert after_duplicate == before + 1, (
        "republishing the same event must add no second message: the message id "
        "is derived from the event, so the stream recognises the duplicate"
    )

    bus.publish(_event(event_id="e-dup-2", idempotency_key="k-dup-2"))

    assert (
        asyncio.run(_stream_messages(nats_env["url"], nats_env["stream"]))
        == after_duplicate + 1
    )


def test_missing_stream_without_management_is_refused(nats_env, monkeypatch):
    monkeypatch.setenv("PIPELINE_NATS_STREAM", "PIPELINE_ABSENT")
    monkeypatch.delenv("PIPELINE_NATS_MANAGE_STREAM", raising=False)

    with pytest.raises(ValueError) as excinfo:
        NatsEventBus()

    assert "PIPELINE_NATS_STREAM" in str(excinfo.value)


def test_the_test_server_url_is_a_loopback_plain_text_url(nats_env):
    parts = urlsplit(nats_env["url"])
    assert parts.scheme == "nats"
    assert parts.hostname == "127.0.0.1"


def test_the_ci_gate_survives_the_suite_env_scrub():
    """``PIPELINE_REQUIRE_NATS_TESTS`` must reach the test modules.

    ``tests/conftest.py`` deletes every ``PIPELINE_*`` variable before
    collection to keep a developer's leaked configuration out of the suite. If
    that scrub also deletes this gate, the broker suites see "not required"
    and skip in CI, which is the silent skip the gate exists to prevent. Run
    for real in a subprocess: the scrub happens at conftest import time, so
    only a separate interpreter can observe it.
    """
    env = {
        **os.environ,
        "PIPELINE_REQUIRE_NATS_TESTS": "1",
        # A path the scrub must still delete, so this case fails if the scrub
        # stops running at all.
        "PIPELINE_NATS_URL": "nats://127.0.0.1:4222",
    }
    conftest_path = Path(__file__).resolve().parents[2] / "tests" / "conftest.py"
    probe = (
        "import importlib.util, os; "
        f"spec = importlib.util.spec_from_file_location('suite_conftest', {str(conftest_path)!r}); "
        "module = importlib.util.module_from_spec(spec); "
        "spec.loader.exec_module(module); "
        "print(os.environ.get('PIPELINE_REQUIRE_NATS_TESTS', '')); "
        "print(os.environ.get('PIPELINE_NATS_URL', ''))"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    lines = (done.stdout.splitlines() + ["", ""])[:2]
    kept, scrubbed = lines
    assert kept == "1", (
        "the suite root scrub deleted PIPELINE_REQUIRE_NATS_TESTS, so the broker "
        "suites would skip instead of failing in CI"
    )
    assert scrubbed == "", (
        "the scrub must still delete runtime configuration such as "
        f"PIPELINE_NATS_URL, got {scrubbed!r}"
    )
