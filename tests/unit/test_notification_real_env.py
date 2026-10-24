"""Routing and delivery against real sockets and files (NC-08).

Nothing in the channel, routing or outbox code is mocked: the Slack channel
talks to real loopback listeners and the multi-tenant run uses a real config
file and real outbox files.  Only stand-in channel *types* replace email and
slack in the end-to-end test, and the plan directory is redirected to tmp_path.
"""

from __future__ import annotations

import http.server
import json
import logging
import shutil
import ssl
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import ClassVar

import pytest

from pipeline import notification_channels as nc
from pipeline import notification_outbox as ob
from pipeline import notification_slack as ns
from pipeline import paths, persistence
from pipeline.notification_channels import DeliveryResult, DeliveryStatus

SECRET_PATH = "/services/T000/B000/SECRETTOKEN"
HOOK_ENV = "NC08_SLACK_HOOK"
RECORD = {"type": "notification", "plan": "p", "payload": {"event": "plan_completed"}}


def _report(capsys, label, measured, expected):
    with capsys.disabled():
        print(f"\n[NC-08] {label}: measured={measured!r} expected={expected!r}")


class _CountingHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.request_count += 1
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@contextmanager
def _listener(tls_context: ssl.SSLContext | None = None) -> Iterator[http.server.HTTPServer]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _CountingHandler)
    server.request_count = 0
    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.daemon = True
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _slack() -> ns.SlackChannelType:
    return ns.SlackChannelType.from_settings(
        "real-slack", {"type": "slack", "webhook_url_env": HOOK_ENV, "timeout": 5}, {}
    )


def _self_signed_context(tmp_path) -> ssl.SSLContext:
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-keyout", str(key), "-out", str(cert), "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    return context


def test_should_not_deliver_to_plain_http_listener_and_send_zero_requests(monkeypatch, capsys):
    with _listener() as server:
        host, port = server.server_address
        monkeypatch.setenv(HOOK_ENV, f"http://{host}:{port}{SECRET_PATH}")

        result = _slack().send(RECORD)

    _report(capsys, "http requests received", server.request_count, 0)
    assert result.status is not DeliveryStatus.DELIVERED
    assert server.request_count == 0


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI not on PATH")
def test_should_retry_and_not_log_url_for_untrusted_certificate(tmp_path, monkeypatch, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    context = _self_signed_context(tmp_path)
    with _listener(context) as server:
        host, port = server.server_address
        url = f"https://{host}:{port}{SECRET_PATH}"
        monkeypatch.setenv(HOOK_ENV, url)

        result = _slack().send(RECORD)

    _report(capsys, "tls status / requests", (result.status.value, server.request_count),
            ("retry", 0))
    assert result.status is DeliveryStatus.RETRY
    assert server.request_count == 0
    assert "SECRETTOKEN" not in caplog.text
    assert url not in caplog.text
    assert f"{host}:{port}" not in caplog.text


class _StandInChannel:
    """Records delivered (plan, event) pairs per instance; status is scripted."""

    type_name = "nc08_fake"
    received: ClassVar[dict] = {}
    status: ClassVar[dict] = {}

    def __init__(self, name):
        self.name = name
        self.outbox_suffix = f".outbox.{name}.jsonl"

    @classmethod
    def from_settings(cls, instance_name, settings, env):
        return cls(instance_name)

    def send(self, record):
        status = self.status.get(self.name, DeliveryStatus.DELIVERED)
        if status is DeliveryStatus.DELIVERED:
            self.received.setdefault(self.name, []).append(
                (record["plan"], record["payload"]["event"])
            )
        return DeliveryResult(status)


CONFIG = {
    "version": 1,
    "instances": {
        "email-a": {"type": "nc08_fake"},
        "slack-a": {"type": "nc08_fake"},
        "email-b": {"type": "nc08_fake"},
        "slack-b": {"type": "nc08_fake"},
    },
    "routes": [
        {
            "name": "team-a",
            "match": {"plan": "team-a-*"},
            "deliver": [
                {"events": ["plan_completed"], "to": ["email-a"]},
                {"events": ["story_parked", "dispatch_failed"], "to": ["slack-a"]},
            ],
        },
        {
            "name": "team-b",
            "match": {"plan": "team-b-*"},
            "deliver": [{"events": ["*"], "to": ["email-b"]}],
        },
    ],
}
EVENTS = ["plan_completed", "story_parked", "dispatch_failed"]
PLANS = ["team-a-build", "team-b-build"]


@pytest.fixture
def tenants(tmp_path, monkeypatch):
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    for module in (paths, persistence, ob):
        monkeypatch.setattr(module, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(nc, "_REGISTRY", {"nc08_fake": _StandInChannel})
    monkeypatch.setattr(_StandInChannel, "received", {})
    monkeypatch.setattr(_StandInChannel, "status", {})
    monkeypatch.delenv(ob.OUTBOX_ENABLED_ENV, raising=False)
    monkeypatch.delenv(ob.OUTBOX_EVENTS_ENV, raising=False)
    config_file = tmp_path / "notifications.json"
    config_file.write_text(json.dumps(CONFIG))
    monkeypatch.setenv("PIPELINE_NOTIFY_CONFIG_PATH", str(config_file))
    for plan in PLANS:
        for event in EVENTS:
            ob.outbox_sink({"type": "notification", "plan": plan, "payload": {"event": event}})
    return plan_dir


def _queued(plan_dir, instance):
    return sum(
        len(f.read_text().splitlines()) for f in plan_dir.glob(f"*.outbox.{instance}.jsonl")
    )


def test_should_route_each_event_to_exactly_its_tenant_instances(tenants, capsys):
    delivered = ob.drain_channel_outboxes()

    received = {k: sorted(v) for k, v in _StandInChannel.received.items()}
    expected = {
        "email-a": [("team-a-build", "plan_completed")],
        "slack-a": [("team-a-build", "dispatch_failed"), ("team-a-build", "story_parked")],
        "email-b": sorted(("team-b-build", e) for e in EVENTS),
    }
    _report(capsys, "delivered counts", delivered,
            {"email-a": 1, "slack-a": 2, "email-b": 3, "slack-b": 0})
    _report(capsys, "received", received, expected)
    assert received == expected
    assert delivered == {"email-a": 1, "slack-a": 2, "email-b": 3, "slack-b": 0}


def test_should_retain_only_the_retrying_instance_queue_across_three_drains(tenants, capsys):
    _StandInChannel.status["slack-a"] = DeliveryStatus.RETRY

    delivered = [ob.drain_channel_outboxes() for _ in range(3)]

    queued = {n: _queued(tenants, n) for n in ("email-a", "slack-a", "email-b", "slack-b")}
    _report(capsys, "queued after 3 drains", queued,
            {"email-a": 0, "slack-a": 2, "email-b": 0, "slack-b": 0})
    _report(capsys, "slack-a delivered per drain", [d["slack-a"] for d in delivered], [0, 0, 0])
    assert queued == {"email-a": 0, "slack-a": 2, "email-b": 0, "slack-b": 0}
    assert [d["slack-a"] for d in delivered] == [0, 0, 0]
    assert "slack-a" not in _StandInChannel.received
    assert len(_StandInChannel.received["email-a"]) == 1
    assert len(_StandInChannel.received["email-b"]) == 3
