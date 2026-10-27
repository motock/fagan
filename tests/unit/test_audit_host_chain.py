"""The host-scoped audit chain (AUDIT-07).

Authentication attempts on the remote MCP transport have no plan to belong to,
so they are chained in a sibling stream: ``<PLAN_DIR>/.audit.host.jsonl``. The
plan-scoped stream keeps its own file per plan; these tests pin the separation,
the hash links, and the rule that an audit failure never changes the decision.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from app.identity import IdentityError, Principal
from app.oidc import OidcProvider
from pipeline import audit, remote_transport
from pipeline.remote_transport import BearerAuthMiddleware

_PRINCIPALS = {
    "alice-token": Principal(subject="alice", auth_method="oidc"),
}


class _StubProvider(OidcProvider):
    """Identity provider double: accepts one known token, rejects the rest."""

    def __init__(self) -> None:
        self._settings = None

    def authenticate(self, credential: str | None) -> Principal:
        principal = _PRINCIPALS.get(credential or "")
        if principal is None:
            raise IdentityError("credential rejected")
        return principal


class _RecordingApp:
    """Downstream ASGI app: records that it was reached, then sends 200."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.seen.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _http_scope(*headers: tuple[bytes, bytes]) -> dict[str, Any]:
    return {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": list(headers),
    }


def _auth(value: str) -> tuple[bytes, bytes]:
    return (b"authorization", value.encode("latin-1"))


def _drive(scope: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run one request through the gate; return (what the app saw, what was sent)."""
    app = _RecordingApp()
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(BearerAuthMiddleware(app, _StubProvider())(scope, receive, send))
    return app.seen, sent


def _recomputed_hash(record: dict[str, Any]) -> str:
    """Recompute a record's hash exactly as ``verify_audit_chain`` does."""
    payload = {key: value for key, value in record.items() if key != "hash"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _host_chain_links_ok(path: Path) -> bool:
    """Check the genesis and per-record links ``verify_audit_chain`` checks."""
    previous_hash = ""
    index = 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("seq") != index:
            return False
        if record.get("prev_hash") != previous_hash:
            return False
        if record.get("hash") != _recomputed_hash(record):
            return False
        previous_hash = str(record.get("hash", ""))
        index += 1
    return True


def _host_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_host_append_writes_only_host_stream(tmp_path: Path, monkeypatch: Any) -> None:
    import pipeline.persistence

    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", tmp_path)
    audit.append_host_audit(action="auth.remote_mcp", outcome="authenticated")
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [".audit.host.jsonl"]


def test_two_host_appends_chain(tmp_path: Path, monkeypatch: Any) -> None:
    import pipeline.persistence

    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", tmp_path)
    first = audit.append_host_audit(action="auth.remote_mcp", outcome="authenticated")
    second = audit.append_host_audit(action="auth.remote_mcp", outcome="denied")
    assert (first["seq"], second["seq"], second["prev_hash"]) == (0, 1, first["hash"])


def test_plan_chain_unaffected_and_host_chain_verifies(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import pipeline.persistence

    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", tmp_path)
    audit.append_audit("p", action="merge", outcome="allowed")
    for _ in range(3):
        audit.append_host_audit(action="auth.remote_mcp", outcome="authenticated")
    assert (
        audit.verify_audit_chain("p") is None,
        _host_chain_links_ok(audit.host_audit_path()),
    ) == (True, True)


def test_plan_append_still_writes_plan_file(tmp_path: Path, monkeypatch: Any) -> None:
    import pipeline.persistence

    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", tmp_path)
    audit.append_audit("p", action="merge", outcome="allowed")
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["p.audit.jsonl"]


def test_host_detail_round_trips(tmp_path: Path, monkeypatch: Any) -> None:
    import pipeline.persistence

    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", tmp_path)
    detail = {"ip": "203.0.113.7", "reason": "bad_token"}
    audit.append_host_audit(action="auth.remote_mcp", outcome="denied", detail=detail)
    assert _host_records(audit.host_audit_path())[-1]["detail"] == detail


def test_middleware_logs_authenticated(tmp_path: Path, monkeypatch: Any) -> None:
    import pipeline.persistence

    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", tmp_path)
    _drive(_http_scope(_auth("Bearer alice-token")))
    assert [
        (record["action"], record["outcome"])
        for record in _host_records(audit.host_audit_path())
    ] == [("auth.remote_mcp", "authenticated")]


def test_middleware_logs_denied(tmp_path: Path, monkeypatch: Any) -> None:
    import pipeline.persistence

    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", tmp_path)
    _seen, sent = _drive(_http_scope(_auth("Bearer s3cr3t-token")))
    assert (
        [record["outcome"] for record in _host_records(audit.host_audit_path())],
        sent[0]["status"],
    ) == (["denied"], 401)


def test_oserror_does_not_block_valid_request(tmp_path: Path, monkeypatch: Any) -> None:
    import pipeline.persistence

    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", tmp_path)

    def _boom(**kwargs: Any) -> dict[str, Any]:
        raise OSError("disk full")

    monkeypatch.setattr(remote_transport, "append_host_audit", _boom)
    seen, _sent = _drive(_http_scope(_auth("Bearer alice-token")))
    assert len(seen) == 1


def test_oserror_does_not_block_denial(tmp_path: Path, monkeypatch: Any) -> None:
    import pipeline.persistence

    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", tmp_path)

    def _boom(**kwargs: Any) -> dict[str, Any]:
        raise OSError("disk full")

    monkeypatch.setattr(remote_transport, "append_host_audit", _boom)
    _seen, sent = _drive(_http_scope(_auth("Bearer s3cr3t-token")))
    assert sent[0]["status"] == 401
