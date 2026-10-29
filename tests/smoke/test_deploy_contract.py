"""Static contract for the smoke stack (``deploy/compose.yaml`` and friends).

``deploy/Dockerfile`` already packages the long-running server image
(``tests/unit/test_deploy_dockerfile.py``). This file covers the *stack* that
makes an end-to-end run possible without a vendor model, a real code host or a
public certificate authority: a Keycloak IdP, a Caddy TLS proxy, a NATS
JetStream broker, the scripted model server and one pipeline container wired
together by ``deploy/compose.yaml``.

``docker compose up`` itself is exercised by ``.github/workflows/smoke.yml``,
not here (the test container has the docker client but no daemon). These tests
read each artifact as text or JSON and assert the invariants that must hold for
*every* revision of the stack:

* no ``latest`` image tag anywhere, so a rebuild is reproducible;
* the proxy terminates TLS with Caddy's internal CA and forwards to the
  pipeline's MCP port, because the pipeline transport has no TLS of its own;
* the IdP runs in dev mode with the realm imported from
  ``deploy/keycloak/realm.json``, which carries the users, the confidential
  dashboard client and the client-credentials client -- and no literal secret;
* the event bus is NATS JetStream with the creds file mounted read-only, and
  no forge is configured, so the merge loop parks at "reviewed";
* the entrypoint seeds the ``git-origin`` volume idempotently and then
  ``exec``s the pipeline command;
* every GitHub Action in the smoke workflow is pinned to a full commit SHA.

They fail on assertion (not on import) before any of the artifacts exist.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"
DOCKERFILE = DEPLOY / "Dockerfile"
ENTRYPOINT = DEPLOY / "entrypoint.sh"
COMPOSE = DEPLOY / "compose.yaml"
REALM = DEPLOY / "keycloak" / "realm.json"
SMOKE = DEPLOY / "smoke" / "smoke.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "smoke.yml"
SCRIPTED_MODEL = REPO_ROOT / "tests" / "smoke" / "scripted_model_server.py"

#: The MCP port the pipeline serves on (``PIPELINE_MCP_PORT`` in deploy/Dockerfile).
MCP_PORT = 8080

#: The services the stack must declare, and none besides: an extra service is
#: an un-reviewed attack surface in a stack that publishes a port.
EXPECTED_SERVICES = {"pipeline", "idp", "proxy", "model", "nats"}

#: Named steps of the smoke run. Each one names the failing step in its trap.
SMOKE_STEPS = (
    "token",
    "list_plans",
    "ingest",
    "approve",
    "dispatch",
    "poll-reviewed",
    "stranger-denied",
    "stranger-no-plans",
    "verify_audit",
    "auth-fail-429",
    "broker-once",
    "restart-no-loss",
)


def _text(path: Path) -> str:
    assert path.exists(), f"missing {path}"
    return path.read_text(encoding="utf-8")


def _compose() -> dict:
    """Parse the subset of YAML this stack uses, without a YAML dependency.

    ``deploy/compose.yaml`` is deliberately plain: block mappings, block
    sequences of scalars, and block scalars. Parsing those here keeps the
    contract test runnable in an environment without PyYAML, which is the same
    environment that has to be able to answer "is the stack wired correctly?"
    before docker is running.
    """
    raw = _text(COMPOSE)
    return _parse_block_yaml(raw)


def _parse_block_yaml(raw: str) -> dict:
    """Parse block mappings/sequences of scalars; ignore comments and blanks."""
    lines = []
    for raw_line in raw.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line.strip() in {"---", "..."}:
            continue
        lines.append(raw_line.rstrip())

    def parse(index: int, indent: int):
        mapping: dict = {}
        sequence: list = []
        is_sequence = lines[index].lstrip().startswith("- ")
        while index < len(lines):
            line = lines[index]
            current = len(line) - len(line.lstrip())
            if current < indent:
                break
            stripped = line.strip()
            if current > indent:
                index += 1
                continue
            if is_sequence:
                if not stripped.startswith("- "):
                    break
                sequence.append(stripped[2:].strip().strip('"').strip("'"))
                index += 1
                continue
            key, _, rest = stripped.partition(":")
            key = key.strip()
            rest = rest.strip()
            if rest in {"|", ">", "|-", ">-"}:
                # Block scalar: consume the more-indented lines verbatim.
                body: list[str] = []
                index += 1
                while index < len(lines):
                    line_indent = len(lines[index]) - len(lines[index].lstrip())
                    if line_indent <= current:
                        break
                    body.append(lines[index].strip())
                    index += 1
                mapping[key] = "\n".join(body)
                continue
            if rest:
                mapping[key] = rest.strip('"').strip("'")
                index += 1
                continue
            if index + 1 < len(lines) and len(lines[index + 1]) - len(lines[index + 1].lstrip()) > current:
                nested_index = index + 1
                child, index = parse(nested_index, len(lines[nested_index]) - len(lines[nested_index].lstrip()))
                mapping[key] = child
                continue
            mapping[key] = None
            index += 1
        return (sequence if is_sequence else mapping), index

    if not lines:
        return {}
    parsed, _ = parse(0, len(lines[0]) - len(lines[0].lstrip()))
    return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------------------
# deploy/Dockerfile
# ---------------------------------------------------------------------------


def _instruction_lines(text: str, keyword: str) -> list[str]:
    """Return the arguments of every ``keyword`` instruction, continuations joined."""
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
    return [item[len(prefix) :].strip() for item in logical if item.upper().startswith(prefix)]


def test_dockerfile_base_image_is_pinned_by_tag() -> None:
    text = _text(DOCKERFILE)
    froms = _instruction_lines(text, "FROM")
    assert froms, "deploy/Dockerfile must declare at least one FROM"
    for instruction in froms:
        image = instruction.split()[0]
        assert ":" in image, f"FROM {image!r} has no explicit version tag"
        tag = image.rsplit(":", 1)[1]
        assert tag and tag != "latest", f"FROM {image!r} must not use latest"


def test_dockerfile_has_no_latest_tag_anywhere() -> None:
    text = _text(DOCKERFILE)
    assert ":latest" not in text, "no image reference may use the latest tag"


def test_dockerfile_runs_as_non_root_and_never_returns_to_root() -> None:
    text = _text(DOCKERFILE)
    users = _instruction_lines(text, "USER")
    assert users, "deploy/Dockerfile must declare a USER"
    last = users[-1].split()[0]
    assert last.split(":")[0] not in {"root", "0"}, f"last USER is {last!r}, not non-root"
    root_later = [u for u in users if u.split()[0].split(":")[0] in {"root", "0"}]
    assert not root_later, f"no USER may go back to root, found {root_later!r}"


# ---------------------------------------------------------------------------
# deploy/entrypoint.sh
# ---------------------------------------------------------------------------


def test_entrypoint_uses_strict_shell_mode() -> None:
    text = _text(ENTRYPOINT)
    # Comments and the shebang are not instructions; the first executable line
    # must be the strict-mode declaration.
    body = [
        line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")
    ]
    assert body, "deploy/entrypoint.sh is empty"
    assert body[0].strip() == "set -euo pipefail", (
        f"entrypoint must open with 'set -euo pipefail', found {body[0].strip()!r}"
    )


def test_entrypoint_guards_git_seeding_before_initialising_the_bare_repo() -> None:
    text = _text(ENTRYPOINT)
    init_at = text.find("git init --bare")
    assert init_at != -1, "entrypoint must seed the bare origin repository"
    guard_at = text.find("if [ ! -e")
    assert guard_at != -1, "the bare-repo seed must be guarded so a restart keeps its refs"
    assert guard_at < init_at, (
        "the idempotency guard must precede 'git init --bare': an unconditional "
        "re-init (or clone --bare) on every start drops the story refs a restart "
        "must preserve"
    )


def test_entrypoint_execs_the_pipeline_command() -> None:
    text = _text(ENTRYPOINT)
    execs = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("exec ")
    ]
    assert execs, "entrypoint must exec the pipeline command so signals and exit status pass through"
    tail = [line.strip() for line in text.splitlines() if line.strip()]
    assert tail[-1].startswith("exec "), (
        f"the last instruction must be the exec'd pipeline command, found {tail[-1]!r}"
    )


# ---------------------------------------------------------------------------
# deploy/compose.yaml
# ---------------------------------------------------------------------------


def test_compose_declares_exactly_the_smoke_services() -> None:
    services = _compose().get("services")
    assert isinstance(services, dict), "deploy/compose.yaml must declare services"
    assert set(services) == EXPECTED_SERVICES, (
        f"service set is {sorted(services)}, expected {sorted(EXPECTED_SERVICES)}"
    )


def test_compose_pins_every_image_tag() -> None:
    services = _compose()["services"]
    for name, spec in services.items():
        image = str(spec.get("image", ""))
        assert image, f"service {name} declares no image"
        assert ":" in image, f"service {name} image {image!r} has no explicit tag"
        tag = image.rsplit(":", 1)[1]
        assert tag != "latest", f"service {name} image must not use latest"


def test_compose_proxy_terminates_tls_internally_and_forwards_to_the_mcp_port() -> None:
    text = _text(COMPOSE)
    assert "tls internal" in text, (
        "the proxy must use Caddy's internal CA: the smoke stack has no public DNS "
        "name to issue a certificate for"
    )
    assert f"reverse_proxy pipeline:{MCP_PORT}" in text, (
        f"the proxy must forward to the pipeline service on {MCP_PORT}; the "
        "pipeline transport has no TLS of its own"
    )


def test_compose_idp_imports_the_realm_in_dev_mode() -> None:
    text = _text(COMPOSE)
    assert "start-dev" in text, "Keycloak must run in dev mode; the stack is not production"
    assert "--import-realm" in text, "the realm must be imported, not created by hand"
    assert "keycloak/realm.json" in text, "the realm file must be mounted into the IdP container"


def test_compose_nats_enables_jetstream_and_keeps_the_broker_private() -> None:
    text = _text(COMPOSE)
    assert "-js" in text, "JetStream must be enabled: acks and redelivery are the contract"
    # The publisher's credential is the broker's reachability, not a token: the
    # broker shares the pipeline container's network namespace, so it is
    # loopback-only and unreachable from every other container and the host.
    assert "network_mode" in text and "service:pipeline" in text, (
        "the nats service must share the pipeline container's network namespace "
        "(loopback-only broker); pipeline/events_nats.py refuses a plaintext URL to "
        "any routable host, and no other container may reach the broker"
    )
    assert not _compose()["services"]["nats"].get("ports"), (
        "the broker must publish no host port: a published JetStream port is a "
        "write path to plan events for anything on the host"
    )
    # When an operator does mount a credential file, it must be read-only.
    mounted = re.findall(r"^\s*-\s*\S*creds\S*:\S+$", text, re.IGNORECASE | re.MULTILINE)
    for mount in mounted:
        assert mount.strip().endswith(":ro"), (
            f"mounted credentials file {mount.strip()!r} must be read-only"
        )


def test_compose_pipeline_selects_the_nats_bus() -> None:
    services = _compose()["services"]
    env = services["pipeline"].get("environment") or {}
    if isinstance(env, list):
        pairs = dict(item.split("=", 1) for item in env)
    else:
        pairs = {str(k): str(v) for k, v in env.items()}
    assert pairs.get("PIPELINE_EVENT_BUS") == "nats", (
        "the pipeline must publish through the broker, not the in-process bus"
    )


def test_compose_declares_and_mounts_the_git_origin_volume() -> None:
    compose = _compose()
    volumes = compose.get("volumes") or {}
    assert "git-origin" in volumes, "a named volume git-origin must be declared"
    text = _text(COMPOSE)
    assert "git-origin:" in text, "the git-origin volume must be mounted by a service"


def test_compose_leaves_the_forge_unconfigured_and_says_why() -> None:
    raw = _text(COMPOSE)
    services = _compose()["services"]
    env = services["pipeline"].get("environment") or {}
    pairs = (
        dict(item.split("=", 1) for item in env)
        if isinstance(env, list)
        else {str(k): str(v) for k, v in env.items()}
    )
    assert not pairs.get("PIPELINE_FORGE"), (
        "no forge may be configured: pipeline/forge.py drives GitHub or GitLab only, "
        "and the smoke stack deliberately stops at 'reviewed, merge parked'"
    )
    assert "PIPELINE_FORGE" in raw, "the file must state the forge invariant in a comment"
    assert "merge" in raw.lower() and "park" in raw.lower(), (
        "the comment must state the merge-park invariant"
    )


# ---------------------------------------------------------------------------
# deploy/keycloak/realm.json
# ---------------------------------------------------------------------------


def _realm() -> dict:
    raw = _text(REALM)
    parsed = json.loads(raw)
    assert isinstance(parsed, dict), "deploy/keycloak/realm.json must be a JSON object"
    return parsed


def test_realm_defines_both_principals() -> None:
    realm = _realm()
    users = {user.get("username") for user in realm.get("users", [])}
    assert {"owner", "stranger"} <= users, f"realm users are {sorted(users)}"


def test_realm_has_one_browser_client_and_one_service_account_client() -> None:
    realm = _realm()
    clients = realm.get("clients", [])
    browser = [c for c in clients if c.get("standardFlowEnabled") is True]
    service = [c for c in clients if c.get("serviceAccountsEnabled") is True]
    assert len(browser) == 1, f"expected exactly one dashboard-login client, got {len(browser)}"
    assert len(service) == 1, f"expected exactly one client-credentials client, got {len(service)}"
    assert browser[0].get("clientId") != service[0].get("clientId"), (
        "the dashboard client and the MCP client must be distinct: one is public-facing "
        "and one carries a service account"
    )
    assert service[0].get("standardFlowEnabled") in (False, None), (
        "the client-credentials client must not also enable the browser flow"
    )


def test_realm_carries_no_literal_secret() -> None:
    realm = _realm()
    for client in realm.get("clients", []):
        secret = client.get("secret")
        if secret is None:
            continue
        assert re.fullmatch(r"\$\{(env\.)?[A-Za-z0-9_]+\}", str(secret)), (
            f"client {client.get('clientId')!r} carries a literal secret; "
            "use a ${ENV_VAR} placeholder and inject it at deploy time"
        )


# ---------------------------------------------------------------------------
# deploy/smoke/smoke.sh
# ---------------------------------------------------------------------------


def test_smoke_script_fails_fast_and_names_the_failing_step() -> None:
    text = _text(SMOKE)
    assert "set -euo pipefail" in text, "smoke.sh must fail fast"
    assert re.search(r"trap\s+'[^']*\$\{?STEP\}?[^']*'[^\n]*ERR", text), (
        "smoke.sh must trap ERR and echo the current step name"
    )
    assert re.search(r"trap\s+'[^']*exit 1", text), "the ERR trap must exit non-zero"


def test_smoke_script_names_every_asserted_step() -> None:
    text = _text(SMOKE)
    missing = [step for step in SMOKE_STEPS if step not in text]
    assert not missing, f"smoke.sh is missing the step(s) {missing}"


# ---------------------------------------------------------------------------
# .github/workflows/smoke.yml
# ---------------------------------------------------------------------------


def test_smoke_workflow_pins_every_action_to_a_commit_sha() -> None:
    raw = _text(WORKFLOW)
    try:
        import yaml
    except ImportError:  # pragma: no cover
        yaml = None
    refs: list[str] = []
    if yaml is not None:
        parsed = yaml.safe_load(raw) or {}
        for job in (parsed.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                if "uses" in step:
                    refs.append(str(step["uses"]))
    if not refs:
        refs = re.findall(r"uses:\s*(\S+)", raw)
    assert refs, "the smoke workflow must use at least one action"
    for ref in refs:
        assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref), (
            f"{ref!r} is not pinned to a full commit SHA (a tag can move under CI)"
        )


# ---------------------------------------------------------------------------
# tests/smoke/scripted_model_server.py
# ---------------------------------------------------------------------------


def test_scripted_model_server_stays_independent_of_the_pipeline() -> None:
    text = _text(SCRIPTED_MODEL)
    forbidden = [line for line in text.splitlines() if re.match(r"\s*(from|import)\s+pipeline", line)]
    assert not forbidden, (
        "the scripted model server must not import pipeline: it runs as a separate "
        f"container and must stay dependency-free, found {forbidden!r}"
    )


# ---------------------------------------------------------------------------
# tests/smoke/scripted_model_server.py -- runtime behaviour, not just its text.
#
# The smoke run's restart step re-polls the model after the pipeline container
# is restarted. A server that indexes its replies with a cursor that runs past
# the end of the script raises IndexError on that re-poll, which the smoke run
# reports as a model failure and which masks the assertion it was actually
# making ("no notification was lost or duplicated"). So the contract is: the
# script is consumed in order and then the LAST reply repeats forever.
# ---------------------------------------------------------------------------


def _load_scripted_model():
    import importlib.util

    spec = importlib.util.spec_from_file_location("scripted_model_server", SCRIPTED_MODEL)
    assert spec is not None and spec.loader is not None, "could not load the scripted model server"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _completion(base_url: str, model: str) -> dict:
    import json as _json
    import urllib.request

    body = _json.dumps(
        {"model": model, "messages": [{"role": "user", "content": "go"}], "stream": False}
    ).encode()
    request = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return _json.loads(response.read().decode())


def test_scripted_model_answers_the_openai_completions_wire_format() -> None:
    module = _load_scripted_model()
    server = module.ScriptedModelServer(port=0).start()
    try:
        reply = _completion(server.base_url, "scripted-dispatch")
        assert reply["object"] == "chat.completion"
        choice = reply["choices"][0]
        assert choice["message"]["role"] == "assistant"
        tools = choice["message"]["tool_calls"]
        assert tools[0]["function"]["name"] == "create_file", (
            f"first dispatch reply must be the create_file harness call, got {tools[0]}"
        )
        # arguments is a JSON-encoded STRING, exactly as the OpenAI API sends it.
        import json as _json

        assert isinstance(tools[0]["function"]["arguments"], str)
        assert _json.loads(tools[0]["function"]["arguments"])["path"].endswith(".txt")
    finally:
        server.stop()


def test_scripted_model_consumes_its_script_in_order_and_then_refuses() -> None:
    """The script is consumed in order; exhaustion is a clean 500, not a crash.

    ``tests/smoke/test_scripted_model_server.py`` freezes this contract
    (exhaustion is terminal: no IndexError, no silent replay of the last reply).
    The smoke run therefore never depends on a fourth model call after a restart:
    ``docker compose restart pipeline`` does not restart the model container, and
    the restart assertion re-reads the notification log and the broker's consumer
    state, not the model.
    """
    module = _load_scripted_model()
    server = module.ScriptedModelServer(port=0).start()
    try:
        names = []
        for _ in range(3):
            reply = _completion(server.base_url, "scripted-dispatch")
            tools = reply["choices"][0]["message"].get("tool_calls") or []
            names.append(tools[0]["function"]["name"] if tools else "")
        assert names == ["create_file", "bash", "done"], names

        import urllib.error

        try:
            _completion(server.base_url, "scripted-dispatch")
        except urllib.error.HTTPError as exc:
            assert exc.code == 500, f"exhaustion must be a clean 500, got {exc.code}"
        else:  # pragma: no cover - the frozen contract says this cannot happen
            raise AssertionError("an exhausted scripted role must not answer 200")
    finally:
        server.stop()
