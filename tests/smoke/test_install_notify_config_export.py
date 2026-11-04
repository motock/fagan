"""SSF-01: ``deploy/install.sh`` must export ``STANDALONE_NOTIFY_CONFIG``.

``compose()`` in ``deploy/install.sh`` runs ``docker compose -f $COMPOSE_REL``
with no ``--env-file``, so compose only sees ``deploy/standalone/.env``. The
installer writes ``STANDALONE_NOTIFY_CONFIG=$STATE_DIR/notification.json`` into
the *state dir's* ``.env``, which is invisible to compose whenever ``--state-dir``
points somewhere else (the standalone smoke uses a ``/tmp`` directory). Compose
then aborts with:

    required variable STANDALONE_NOTIFY_CONFIG is missing a value
    (deploy/standalone/compose.yaml:121)

The installer already solves exactly this for the realm file by exporting
``STANDALONE_REALM_FILE`` in its own shell just before ``compose up``; the
notification config needs the same treatment.

These checks are static text checks on the script: no Docker daemon, no network.
"""

from __future__ import annotations

from pathlib import Path

# parents[2] is the repo root: tests/smoke/<this file> -> tests/ -> <repo root>.
INSTALL = Path(__file__).resolve().parents[2] / "deploy" / "install.sh"

EXPORT_PREFIX = "export STANDALONE_NOTIFY_CONFIG="

# The real invocation (column 0, line 794 pre-change). The bare substring also
# appears inside the --dry-run printf at line 766
# ('STANDALONE_REALM_FILE=%s docker compose -f %s up -d --build --wait'), so it
# is anchored on its own line to stay unique.
INVOCATION = "\ncompose up -d --build --wait\n"


def _script() -> str:
    assert INSTALL.is_file(), f"missing installer: {INSTALL}"
    return INSTALL.read_text(encoding="utf-8")


def _export_line(text: str) -> str:
    matches = [
        line for line in text.splitlines() if line.strip().startswith(EXPORT_PREFIX)
    ]
    assert len(matches) == 1, (
        f"expected exactly one `{EXPORT_PREFIX}` line in {INSTALL}, got {len(matches)}"
    )
    return matches[0]


def test_installer_exports_standalone_notify_config() -> None:
    text = _script()
    assert EXPORT_PREFIX in text, (
        f"{INSTALL} never exports STANDALONE_NOTIFY_CONFIG; compose() runs without "
        "--env-file, so ${STANDALONE_NOTIFY_CONFIG:?...} in compose.yaml is unset "
        "whenever --state-dir is not deploy/standalone"
    )
    _export_line(text)


def test_notify_config_export_precedes_compose_up() -> None:
    text = _script()
    assert text.count(INVOCATION) == 1, (
        f"expected exactly one real `compose up -d --build --wait` invocation in "
        f"{INSTALL}; the ordering anchor {INVOCATION!r} matched "
        f"{text.count(INVOCATION)} times"
    )
    export_index = text.index(_export_line(text))
    invocation_index = text.index(INVOCATION)
    assert export_index < invocation_index, (
        "STANDALONE_NOTIFY_CONFIG is exported after `compose up -d --build --wait` "
        f"(export at offset {export_index}, invocation at {invocation_index}); "
        "compose aborts before the export ever runs"
    )


def test_notify_config_export_uses_notify_config_file() -> None:
    text = _script()
    line = _export_line(text)
    value = line.split("=", 1)[1].strip()
    assert "NOTIFY_CONFIG_FILE" in value, (
        f"`{line.strip()}` does not reference $NOTIFY_CONFIG_FILE; compose must get "
        "the state dir's notification.json, and NOTIFY_CONFIG_FILE already holds it"
    )
    assert "/" not in value, (
        f"`{line.strip()}` hardcodes a path instead of referencing "
        "$NOTIFY_CONFIG_FILE"
    )


def test_realm_file_export_is_still_present() -> None:
    text = _script()
    assert "export STANDALONE_REALM_FILE=" in text, (
        f"{INSTALL} no longer exports STANDALONE_REALM_FILE; the notify fix must "
        "sit alongside the realm export, not replace it"
    )
