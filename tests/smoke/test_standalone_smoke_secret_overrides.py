"""SSF-01: the smoke override must re-point *every* compose secret.

``deploy/standalone/compose.yaml`` mounts secret files as ``./secrets/<name>``,
i.e. relative to ``deploy/standalone``. The installer writes them under the
throwaway state dir instead (``--state-dir /tmp/...``), so
``deploy/standalone/smoke.sh`` writes a compose override that re-points each
secret at ``$STATE_DIR/secrets/<name>``.

That override's loop is a hardcoded list of names, so a secret added to the base
file and to install.sh's ``COMPOSE_SECRETS`` can still be missed here. When that
happens the stack fails with:

    bind source path does not exist: .../deploy/standalone/secrets/<name>

(the failure that blocked the ``Standalone smoke`` job: notify_email_password and
slack_webhook_url were added to the base file but not to the loop).

These are static text checks on the two scripts: no Docker daemon, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE = REPO_ROOT / "deploy" / "standalone" / "smoke.sh"
INSTALL = REPO_ROOT / "deploy" / "install.sh"

# `COMPOSE_SECRETS="a b c"` in install.sh: the single source of truth for the
# secret files the installer materialises in the state dir.
COMPOSE_SECRETS = re.compile(r'^COMPOSE_SECRETS="([^"]*)"', re.MULTILINE)

# `for name in a b c; do` inside smoke.sh's override writer.
OVERRIDE_LOOP = re.compile(r"^\s*for name in ([^;]+); do", re.MULTILINE)

# The two secrets compose.yaml names but the override loop used to miss.
REGRESSION_SECRETS = ("notify_email_password", "slack_webhook_url")

# The `cleanup() { ... }` function body in smoke.sh (the closing brace is the only
# one in column 0; every nested block is indented).
CLEANUP_FUNCTION = re.compile(r"^cleanup\(\) \{\n(.*?)^\}\n", re.MULTILINE | re.DOTALL)

# `if [ "$status" -ne 0 ]; then ... fi`: the guard that keeps a *failed* run's
# evidence in the job output and a green run's output quiet.
# `${VAR:?message}` in compose.yaml: compose refuses to interpolate the project
# (even for `ps` or `logs`) unless VAR is set, so cleanup() must export each one
# before it dumps evidence.
REQUIRED_COMPOSE_VAR = re.compile(r"\$\{([A-Z_][A-Z0-9_]*):\?")

STATUS_GUARD = re.compile(
    r'if \[ "\$status" -ne 0 \]; then\n(?P<body>.*?)\n\s*fi\b', re.DOTALL
)


def _text(path: Path) -> str:
    assert path.is_file(), f"missing script: {path}"
    return path.read_text(encoding="utf-8")


def _installer_secrets() -> list[str]:
    match = COMPOSE_SECRETS.search(_text(INSTALL))
    assert match, 'deploy/install.sh has no COMPOSE_SECRETS="..." assignment'
    return match.group(1).split()


def _override_loop_names() -> list[str]:
    text = _text(SMOKE)
    # The override writer is the loop whose body re-points a secret file into
    # the state dir; there may be other `for` loops in the script.
    for match in OVERRIDE_LOOP.finditer(text):
        body = text[match.end() : text.find("\ndone", match.end())]
        if "secrets/$name" in body:
            return match.group(1).split()
    raise AssertionError(
        "deploy/standalone/smoke.sh has no `for name in ...; do` loop that "
        "re-points $STATE_DIR/secrets/$name"
    )


def test_override_loop_names_the_notification_secrets():
    names = _override_loop_names()
    missing = [name for name in REGRESSION_SECRETS if name not in names]
    assert not missing, (
        "deploy/standalone/smoke.sh's override loop must name "
        f"{list(REGRESSION_SECRETS)}; missing {missing} makes `docker compose up` "
        "fail with 'bind source path does not exist: "
        "deploy/standalone/secrets/<name>'"
    )


def test_override_loop_covers_every_installer_secret():
    installer = _installer_secrets()
    assert installer, "COMPOSE_SECRETS in deploy/install.sh parsed as empty"
    names = _override_loop_names()
    missing = [name for name in installer if name not in names]
    assert not missing, (
        "every secret in deploy/install.sh's COMPOSE_SECRETS must also be "
        f"re-pointed by deploy/standalone/smoke.sh's override loop; missing {missing}"
    )


# ---------------------------------------------------------------------------
# CHANGE 3: a failed run must leave its evidence in the job output
# ---------------------------------------------------------------------------


def _cleanup_body() -> str:
    match = CLEANUP_FUNCTION.search(_text(SMOKE))
    assert match, "deploy/standalone/smoke.sh has no `cleanup() {` function"
    return match.group(1)


def test_cleanup_dumps_logs_of_the_failing_services():
    body = _cleanup_body()
    tail_calls = re.findall(r"logs --tail[^\n]*", body)
    assert tail_calls, (
        "cleanup() must run `docker compose ... logs --tail N <services>` on a "
        "failed run; without it the IdP-probe failure leaves no evidence (the "
        "workflow's own log step dies on compose interpolation)"
    )
    dump = " ".join(tail_calls)
    missing = [name for name in ("keycloak", "proxy") if name not in dump]
    assert not missing, (
        f"the failure log dump in cleanup() must include {missing}: the IdP probe "
        "fails inside keycloak, behind the proxy"
    )


def _cleanup_code_lines() -> list[tuple[int, str]]:
    """(offset, line) for the non-comment lines of cleanup(), comments skipped.

    Both the preamble and the block comment the `down -v` choice in prose, so a
    raw substring search finds the explanation rather than the call.
    """
    lines: list[tuple[int, str]] = []
    offset = 0
    for line in _cleanup_body().splitlines(keepends=True):
        if not line.lstrip().startswith("#"):
            lines.append((offset, line))
        offset += len(line)
    return lines


def test_cleanup_dumps_logs_before_teardown():
    code = _cleanup_code_lines()
    tail_at = next((off for off, line in code if "logs --tail" in line), None)
    down_at = next((off for off, line in code if "down -v" in line), None)
    assert tail_at is not None, "cleanup() has no `logs --tail` failure dump"
    assert down_at is not None, "cleanup() has no `down -v` teardown"
    assert tail_at < down_at, (
        "the `logs --tail` dump must run BEFORE `down -v` in cleanup(): "
        "`down -v` removes the containers, so a dump after it prints nothing"
    )


def test_cleanup_log_dump_is_guarded_by_a_failed_status():
    body = _cleanup_body()
    guards = [
        match
        for match in STATUS_GUARD.finditer(body)
        if "logs --tail" in match.group("body")
    ]
    assert guards, (
        'the log dump in cleanup() must sit inside `if [ "$status" -ne 0 ]; then '
        "... fi`, so a green run does not print 200 lines of noise per service"
    )


def test_cleanup_tears_down_exactly_once():
    body = _cleanup_body()
    # Comment lines are not calls: cleanup()'s preamble explains the `down -v`
    # choice in prose, and only indented command lines can actually tear down.
    calls = [
        line
        for line in body.splitlines()
        if "down -v" in line and not line.lstrip().startswith("#")
    ]
    assert len(calls) == 1, (
        "cleanup() must call `down -v` exactly once (the STANDALONE_SMOKE_KEEP "
        "guard and the failure dump must not add a second teardown): "
        f"{calls} found"
    )


def _body_lines(body: str) -> list[tuple[int, str]]:
    """(offset, line) for every line of a function body; the offset is a char index."""
    lines: list[tuple[int, str]] = []
    offset = 0
    for line in body.splitlines(keepends=True):
        lines.append((offset, line))
        offset += len(line)
    return lines


def test_cleanup_exports_the_rendered_realm_file():
    """compose.yaml:157 needs the realm path, or the failure dump prints nothing.

    The first CHANGE 3 attempt only satisfied STANDALONE_NOTIFY_CONFIG, so both
    `compose ps` and `compose logs` aborted with "required variable
    STANDALONE_REALM_FILE is missing a value" and the `|| true` swallowed it:
    the job printed the log() headers and nothing else.
    """
    body = _cleanup_body()
    line = next(
        (ln for ln in body.splitlines() if "export STANDALONE_REALM_FILE=" in ln),
        None,
    )
    assert line is not None, (
        "cleanup() must export STANDALONE_REALM_FILE: deploy/standalone/compose.yaml "
        "declares it with `${VAR:?}`, so the failure dump dies on interpolation and "
        "leaves no evidence"
    )
    assert "$STATE_DIR/import/realm.json" in line, (
        "cleanup() must point STANDALONE_REALM_FILE at the realm install.sh rendered "
        f"into the state dir ($STATE_DIR/import/realm.json), not a fixed path: {line!r}"
    )
    assert "deploy/standalone/" not in line, (
        f"STANDALONE_REALM_FILE must not name the repo's committed path: {line!r}"
    )


def test_cleanup_satisfies_every_required_compose_variable():
    """Any new `${VAR:?}` in the base file must not silently kill the dump."""
    compose = REPO_ROOT / "deploy" / "standalone" / "compose.yaml"
    required = REQUIRED_COMPOSE_VAR.findall(_text(compose))
    assert required, "deploy/standalone/compose.yaml declares no `${VAR:?}` variables"
    body = _cleanup_body()
    dump_at = body.find("logs --tail")
    assert dump_at != -1, "cleanup() has no `logs --tail` dump to guard"
    for name in sorted(set(required)):
        # `export FOO=...`, `export FOO` and `export FOO BAR` all count: cleanup()
        # sets the values in plain assignments first and only then exports them.
        exported_at = max(
            (
                offset
                for offset, line in _body_lines(body)
                if re.search(rf"\bexport\b[^\n]*\b{name}\b", line)
            ),
            default=-1,
        )
        assert exported_at != -1, (
            f"cleanup() must export {name}: deploy/standalone/compose.yaml declares it "
            "with `${VAR:?}`, so `compose ps`/`compose logs` abort on interpolation "
            "and a failed run leaves no evidence"
        )
        assert exported_at < dump_at, (
            f"cleanup() must export {name} BEFORE the `logs --tail` dump, otherwise "
            "the dump aborts on interpolation"
        )
