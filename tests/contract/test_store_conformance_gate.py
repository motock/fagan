"""The published store contract suite works as an external package's gate.

pipeline/testing/store_conformance.py is what an out-of-tree store package (for
example a Postgres store) runs in its own CI (ADR 0012 Consequences: a suite that
only FileStore passes proves nothing). These tests drive a minimal external
package through pytester:

* a conforming store passes every published test;
* a store that accepts foreign namespaces fails and names the namespace test;
* importing the published module does not load ``pipeline.server``.
"""

import subprocess
import sys
from pathlib import Path

pytest_plugins = ("pytester",)

REPO_ROOT = Path(__file__).resolve().parents[2]

CONFTEST = """\
import pytest

from pipeline import persistence
from pipeline.store_memory import InMemoryRecordStore


@pytest.fixture
def record_store_factory(tmp_path, monkeypatch):
    # The store reads plan drafts from the plans directory; keep it empty.
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    return InMemoryRecordStore()


@pytest.fixture
def record_store_serves_other():
    return frozenset(
        {"manifest", "decisions", "journal", "usage", "notification", "context_snapshot"}
    )


@pytest.fixture
def record_store_known_gaps():
    return {
        "malformed_get": "get_context_snapshot returns None for a malformed digest",
    }
"""

TEST_MODULE = """\
from pipeline.testing.store_conformance import *  # noqa: F401,F403
"""

LEAKY_CONFTEST = """\
import pytest

from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE, RECORD_STORE_METHODS


class _FoldIntoDefault:
    def __init__(self):
        self._inner = InMemoryRecordStore()

    def __getattr__(self, name):
        if name not in RECORD_STORE_METHODS:
            raise AttributeError(name)
        method = getattr(self._inner, name)

        def fold(namespace, *args, **kwargs):
            return method(DEFAULT_NAMESPACE, *args, **kwargs)

        return fold


@pytest.fixture
def record_store_factory():
    return _FoldIntoDefault()
"""


def test_a_conforming_external_store_passes_the_published_suite(pytester):
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(test_store_conformance=TEST_MODULE)

    result = pytester.runpytest("-p", "no:cacheprovider")

    outcomes = result.parseoutcomes()
    assert outcomes.get("failed", 0) == 0
    assert outcomes.get("errors", 0) == 0
    assert outcomes.get("passed", 0) > 0
    assert result.ret == 0


def test_a_store_accepting_foreign_namespaces_fails_the_namespace_test_by_name(pytester):
    pytester.makeconftest(LEAKY_CONFTEST)
    pytester.makepyfile(test_store_conformance=TEST_MODULE)

    result = pytester.runpytest("-p", "no:cacheprovider", "-v")

    assert result.ret != 0
    result.stdout.fnmatch_lines(["*test_a_foreign_namespace*FAILED*"])


def test_importing_the_published_suite_does_not_load_the_server():
    probe = (
        "import sys, pipeline.testing.store_conformance; "
        "print('pipeline.server' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"
