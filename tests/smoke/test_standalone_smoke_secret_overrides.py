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
