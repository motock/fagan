"""Tests for the deploy/standalone/mcp-token client-credentials helper.

The helper is a standalone stdlib-only script shipped with the server image,
not an importable module, so it is loaded by path with SourceFileLoader. Token
responses come from a local http.server thread; every success path passes
``--insecure-http`` because the helper refuses plain http token_urls.
"""

from __future__ import annotations

import base64
import importlib.machinery
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "standalone" / "mcp-token"


def load_helper():
    """Load the mcp-token script as a fresh module (never cached in sys.modules)."""
    assert SCRIPT.is_file(), f"missing implementation file: {SCRIPT}"
    loader = importlib.machinery.SourceFileLoader(
        "mcp_token_helper_under_test", str(SCRIPT)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class _TokenHandler(BaseHTTPRequestHandler):
    """Records each request and answers with the server's configured response."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8")
        self.server.requests.append({"headers": dict(self.headers), "body": body})
        raw = getattr(self.server, "raw_body", None)
        payload = raw if raw is not None else json.dumps(self.server.body).encode("utf-8")
        self.send_response(self.server.status)
        location = getattr(self.server, "location", None)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture
def token_server():
    server = HTTPServer(("127.0.0.1", 0), _TokenHandler)
    server.status = 200
    server.body = {"access_token": "tok-123", "token_type": "Bearer"}
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.url = f"http://127.0.0.1:{server.server_port}/token"
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def write_config(tmp_path, url, **overrides):
    """Write a 0600 credentials file (chmod after write: new files land 0644)."""
    config = {
        "token_url": url,
        "client_id": "client-id",
        "client_secret": "client-secret",
    }
    config.update(overrides)
    cfg_path = tmp_path / "mcp-client.json"
    cfg_path.write_text(json.dumps(config))
    cfg_path.chmod(0o600)
    return cfg_path


def test_prints_json_header_object(token_server, tmp_path, capsys):
    cfg = write_config(tmp_path, token_server.url)
    helper = load_helper()

    assert helper.main(["--config", str(cfg), "--insecure-http"]) == 0
    assert capsys.readouterr().out == '{"Authorization": "Bearer tok-123"}\n'


def test_raw_prints_only_the_token(token_server, tmp_path, capsys):
    cfg = write_config(tmp_path, token_server.url)
    helper = load_helper()

    assert helper.main(["--config", str(cfg), "--raw", "--insecure-http"]) == 0
    assert capsys.readouterr().out == "tok-123\n"


def test_group_readable_config_is_refused(token_server, tmp_path, capsys):
    cfg = write_config(tmp_path, token_server.url)
    cfg.chmod(0o644)
    helper = load_helper()

    assert helper.main(["--config", str(cfg), "--insecure-http"]) == 2
    captured = capsys.readouterr()
    assert str(cfg) in captured.err
    assert "client-secret" not in captured.err
    assert "tok-" not in captured.out
    assert captured.out == ""
    assert token_server.requests == []


def test_missing_key_is_refused(token_server, tmp_path, capsys):
    cfg = write_config(tmp_path, token_server.url, client_secret=None)
    helper = load_helper()

    assert helper.main(["--config", str(cfg), "--insecure-http"]) == 2
    captured = capsys.readouterr()
    assert "client_secret" in captured.err
    assert captured.out == ""


def test_plain_http_token_url_is_refused(token_server, tmp_path, capsys):
    cfg = write_config(tmp_path, token_server.url)
    helper = load_helper()

    assert helper.main(["--config", str(cfg)]) == 2
    captured = capsys.readouterr()
    assert "token_url" in captured.err
    assert captured.out == ""


def test_http_error_does_not_leak_body_and_recovers(token_server, tmp_path, capsys):
    cfg = write_config(tmp_path, token_server.url)
    token_server.status = 401
    token_server.body = {"error": "invalid_client", "echo": "client-secret"}
    helper = load_helper()

    assert helper.main(["--config", str(cfg), "--insecure-http"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.strip().splitlines()) == 1
    assert "client-secret" not in captured.err
    assert "invalid_client" not in captured.err

    token_server.status = 200
    token_server.body = {"access_token": "tok-abc", "token_type": "Bearer"}
    assert helper.main(["--config", str(cfg), "--insecure-http"]) == 0
    assert capsys.readouterr().out == '{"Authorization": "Bearer tok-abc"}\n'


def test_response_without_access_token_fails(token_server, tmp_path, capsys):
    cfg = write_config(tmp_path, token_server.url)
    token_server.body = {"token_type": "Bearer", "expires_in": 300}
    helper = load_helper()

    assert helper.main(["--config", str(cfg), "--insecure-http"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.strip().splitlines()) == 1


def test_request_uses_basic_auth_and_client_credentials_grant(
    token_server, tmp_path
):
    cfg = write_config(tmp_path, token_server.url)
    helper = load_helper()

    assert helper.main(["--config", str(cfg), "--insecure-http"]) == 0
    assert len(token_server.requests) == 1
    request = token_server.requests[0]
    expected_basic = base64.b64encode(b"client-id:client-secret").decode()
    assert request["headers"]["Authorization"] == f"Basic {expected_basic}"
    assert request["headers"]["Content-Type"].startswith(
        "application/x-www-form-urlencoded"
    )
    assert parse_qs(request["body"])["grant_type"] == ["client_credentials"]


def test_config_path_from_environment(token_server, tmp_path, monkeypatch, capsys):
    cfg = write_config(tmp_path, token_server.url)
    monkeypatch.setenv("PIPELINE_MCP_CREDENTIALS", str(cfg))
    helper = load_helper()

    assert helper.main(["--insecure-http"]) == 0
    assert capsys.readouterr().out == '{"Authorization": "Bearer tok-123"}\n'


def test_non_utf8_response_body_fails_with_one_line(token_server, tmp_path, capsys):
    """A non-UTF-8 body must be a one-line exit 1, not a UnicodeDecodeError traceback."""
    cfg = write_config(tmp_path, token_server.url)
    token_server.status = 200
    token_server.raw_body = b'{"access_token": "\xff\xfe"}'
    helper = load_helper()

    assert helper.main(["--config", str(cfg), "--insecure-http"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.strip().splitlines()) == 1


def test_insecure_http_is_refused_for_non_loopback_hosts(tmp_path, capsys):
    cfg = write_config(tmp_path, "http://idp.example.test/token")
    helper = load_helper()

    assert helper.main(["--config", str(cfg), "--insecure-http"]) == 2
    captured = capsys.readouterr()
    assert "token_url" in captured.err
    assert captured.out == ""
    assert "client-secret" not in captured.err


class _RecordingHandler(BaseHTTPRequestHandler):
    """Would hand out a token: it must never be reached after a redirect."""

    def _record_and_answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8")
        self.server.requests.append(
            {"method": self.command, "headers": dict(self.headers), "body": body}
        )
        payload = json.dumps({"access_token": "tok-stolen"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # urllib downgrades a redirected POST to a GET, so both verbs must be
    # recorded: missing the GET here would hide the credential leak.
    do_POST = _record_and_answer
    do_GET = _record_and_answer

    def log_message(self, *args):  # keep pytest output clean
        pass


def test_redirect_is_refused_and_never_reaches_the_other_origin(tmp_path, capsys):
    """A 302 must be a hard failure: the Basic secret must not reach the target."""
    sink = HTTPServer(("127.0.0.1", 0), _RecordingHandler)
    sink.requests = []
    sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
    sink_thread.start()

    origin = HTTPServer(("127.0.0.1", 0), _TokenHandler)
    origin.status = 302
    origin.body = {"access_token": "tok-ignored"}
    origin.location = f"http://127.0.0.1:{sink.server_port}/steal"
    origin.requests = []
    origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
    origin_thread.start()
    try:
        cfg = write_config(
            tmp_path, f"http://127.0.0.1:{origin.server_port}/token"
        )
        helper = load_helper()

        assert helper.main(["--config", str(cfg), "--insecure-http"]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert len(captured.err.strip().splitlines()) == 1
        assert "client-secret" not in captured.err
        assert "client-secret" not in captured.out
        assert base64.b64encode(b"client-id:client-secret").decode() not in (
            captured.err
        )
        assert len(origin.requests) == 1
        assert sink.requests == []
    finally:
        for server, thread in ((origin, origin_thread), (sink, sink_thread)):
            server.shutdown()
            server.server_close()
            thread.join()
