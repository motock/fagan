"""The anchor status ``/api/health`` reports (ADR 0012 decision 10, Q1).

A deployment that stores its audit trail somewhere the pipeline cannot rewrite
has an external anchor; a deployment that does not has none.  The health
payload says which, with one fixed-word field, so an operator can see it
without reading the logs:

* ``configured`` -- an anchor sink is configured and usable;
* ``unconfigured`` -- no sink is configured, and this deployment is not the
  file store (so the audit trail has no external anchor at all);
* ``off`` -- no sink is configured and this deployment is the file store;
* ``invalid`` -- a sink is configured but the value cannot be used.

The value is a fixed word.  It never carries the configured value, the sink's
path, or the text of the failure: a health payload is read by anything that
can reach the API, and the anchor's location is exactly the thing that must
not leak there.

The verdict is additive.  It never turns the instance unhealthy and never
adds a degraded reason -- anchoring is a property of the deployment, not a
fault in the running process -- so a store without an anchor still starts and
still reports itself healthy.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline import health_freshness

ANCHOR_VARIABLE = "PIPELINE_AUDIT_ANCHOR"
STORE_VARIABLE = "PIPELINE_STORE"
STORE_PATH_VARIABLE = "PIPELINE_STORE_PATH"

#: The four fixed words the field can carry.
VALUES = ("configured", "unconfigured", "off", "invalid")

REFERENCE = Path(__file__).resolve().parents[2] / "REFERENCE.md"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from "no anchor, file store"."""
    monkeypatch.delenv(ANCHOR_VARIABLE, raising=False)
    monkeypatch.delenv(STORE_VARIABLE, raising=False)
    monkeypatch.delenv(STORE_PATH_VARIABLE, raising=False)


@pytest.fixture()
def sqlite_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Select the sqlite store, with a usable database path."""
    database = tmp_path / "pipeline.db"
    monkeypatch.setenv(STORE_VARIABLE, "sqlite")
    monkeypatch.setenv(STORE_PATH_VARIABLE, str(database))
    return str(database)


@pytest.fixture()
def anchor_sink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point the anchor at a file outside the plan directory."""
    path = tmp_path / "anchor" / "heads.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(ANCHOR_VARIABLE, f"file:{path}")
    return str(path)


def _anchor(tmp_path: Path) -> str:
    """The anchor field of the health payload."""
    return health_freshness.health_extras(tmp_path)["anchor"]


# --------------------------------------------------------------------------
# One test per value
# --------------------------------------------------------------------------


def test_anchor_is_configured_when_a_sink_is_set(
    tmp_path: Path, anchor_sink: str
) -> None:
    assert _anchor(tmp_path) == "configured"


def test_anchor_is_off_on_the_file_store_without_a_sink(tmp_path: Path) -> None:
    assert _anchor(tmp_path) == "off"


def test_anchor_is_unconfigured_without_a_sink_on_another_store(
    tmp_path: Path, sqlite_store: str
) -> None:
    assert _anchor(tmp_path) == "unconfigured"


def test_anchor_is_invalid_when_the_configured_value_is_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ANCHOR_VARIABLE, "s3://user:secret@bucket")

    assert _anchor(tmp_path) == "invalid"


# --------------------------------------------------------------------------
# The value is a fixed word: no path, no value, no exception text
# --------------------------------------------------------------------------


def test_the_anchor_field_never_carries_the_configured_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad value may hold a credential, and it must not reach the payload."""
    monkeypatch.setenv(ANCHOR_VARIABLE, "s3://user:secret@bucket")

    payload = json.dumps(health_freshness.health_extras(tmp_path))

    assert "secret" not in payload
    assert "bucket" not in payload


def test_the_anchor_field_never_carries_the_sink_path(
    tmp_path: Path, anchor_sink: str
) -> None:
    payload = json.dumps(health_freshness.health_extras(tmp_path))

    assert anchor_sink not in payload


def test_the_anchor_value_is_one_of_the_four_fixed_words(
    tmp_path: Path, anchor_sink: str
) -> None:
    assert _anchor(tmp_path) in VALUES


# --------------------------------------------------------------------------
# The verdict is additive: it never degrades the instance
# --------------------------------------------------------------------------


def test_an_unconfigured_anchor_leaves_the_instance_healthy(
    tmp_path: Path, sqlite_store: str
) -> None:
    result = health_freshness.health_extras(tmp_path)

    assert result["anchor"] == "unconfigured"
    assert result["healthy"] is True
    assert result["degraded"] == []


def test_an_invalid_anchor_leaves_the_instance_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ANCHOR_VARIABLE, "s3://user:secret@bucket")

    result = health_freshness.health_extras(tmp_path)

    assert result["anchor"] == "invalid"
    assert result["healthy"] is True
    assert result["degraded"] == []


def test_a_configured_anchor_leaves_the_instance_healthy(
    tmp_path: Path, anchor_sink: str
) -> None:
    result = health_freshness.health_extras(tmp_path)

    assert result["anchor"] == "configured"
    assert result["healthy"] is True
    assert result["degraded"] == []


def test_an_off_anchor_leaves_the_instance_healthy(tmp_path: Path) -> None:
    result = health_freshness.health_extras(tmp_path)

    assert result["anchor"] == "off"
    assert result["healthy"] is True
    assert result["degraded"] == []


# --------------------------------------------------------------------------
# A store selection the pipeline cannot honour is still reported, not raised
# --------------------------------------------------------------------------


def test_an_unusable_store_selection_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unsupported store is reported as "invalid", which is not "file".

    The kind in force is therefore not the file store, so the anchor is
    "unconfigured" -- and the probe still answers.
    """
    monkeypatch.setenv(STORE_VARIABLE, "not-a-store")

    result = health_freshness.health_extras(tmp_path)

    assert result["anchor"] == "unconfigured"
    assert result["healthy"] is True


# --------------------------------------------------------------------------
# One warning per process, naming the store kind only
# --------------------------------------------------------------------------


def test_an_unconfigured_anchor_warns_once_per_process(
    tmp_path: Path,
    sqlite_store: str,
) -> None:
    """The warning is logged once, however many times the probe is asked.

    A health probe runs on every request; a warning per request would flood
    the log, so the notice is emitted once per process.  The two calls run in
    a fresh interpreter, so the count is about the notice and not about what
    an earlier test in this worker happened to log.
    """
    script = (
        "import logging, sys\n"
        "logging.basicConfig(level=logging.WARNING, stream=sys.stderr)\n"
        "from pipeline import health_freshness\n"
        "health_freshness.health_extras(sys.argv[1])\n"
        "health_freshness.health_extras(sys.argv[1])\n"
    )
    env = dict(os.environ)
    env[STORE_VARIABLE] = "sqlite"
    env[STORE_PATH_VARIABLE] = sqlite_store
    env.pop(ANCHOR_VARIABLE, None)

    proc = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(Path(__file__).resolve().parents[2]),
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    warnings = [
        line
        for line in proc.stderr.splitlines()
        if "WARNING" in line and "sqlite" in line.lower()
    ]
    assert len(warnings) == 1, proc.stderr


# --------------------------------------------------------------------------
# The documented contract
# --------------------------------------------------------------------------


def test_reference_documents_the_anchor_field_and_its_four_values() -> None:
    """REFERENCE.md must name the field and every value it can carry."""
    text = REFERENCE.read_text(encoding="utf-8")

    assert "anchor: unconfigured" in text
    for value in VALUES:
        assert re.search(rf"\b{value}\b", text), (
            f"REFERENCE.md does not name the anchor value {value!r}"
        )
