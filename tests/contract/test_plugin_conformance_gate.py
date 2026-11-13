"""The published outbox, audit and transaction suites work as a plugin's gate.

pipeline/testing/outbox_conformance.py, audit_conformance.py and
transaction_conformance.py are what an out-of-tree store package runs in its own
CI, alongside pipeline/testing/store_conformance.py. These tests drive a minimal
external package through pytester:

* a conforming store passes every test in the three modules;
* a store whose ``ack_outbox`` removes more than the acked entry fails the
  outbox test by name;
* an audit log that re-hashes its history on append fails the tamper test by
  name;
* the tamper hook and the transaction tier are required fixtures, and an
  unknown tier value is an error;
* importing each module does not load ``pipeline.server``.
"""

import subprocess
import sys
from pathlib import Path

import pytest

pytest_plugins = ("pytester",)

REPO_ROOT = Path(__file__).resolve().parents[2]

MODULES = (
    "pipeline.testing.outbox_conformance",
    "pipeline.testing.audit_conformance",
    "pipeline.testing.transaction_conformance",
)

PLAIN_STORE = """\
class Store(InMemoryRecordStore):
    pass
"""

ACK_EVERYTHING_STORE = """\
class Store(InMemoryRecordStore):
    def ack_outbox(self, namespace, plan_name, channel, ids):
        every = [entry["id"] for entry in self.read_outbox(namespace, plan_name, channel)]
        return super().ack_outbox(namespace, plan_name, channel, every)
"""

REHASHING_STORE = """\
from pipeline.audit_chain import link
from pipeline.audit_memory import InMemoryAuditLog


class RehashingAuditLog(InMemoryAuditLog):
    def append(self, namespace, chain, body):
        record = super().append(namespace, chain, body)
        with self._lock:
            rebuilt = []
            for entry in self._chains[namespace][chain]:
                fields = {k: v for k, v in entry.items() if k not in ("seq", "prev_hash", "hash")}
                rebuilt.append(link(rebuilt[-1] if rebuilt else None, fields))
            self._chains[namespace][chain] = rebuilt
        return record


class Store(InMemoryRecordStore):
    def __init__(self):
        super().__init__()
        self._audit_log = RehashingAuditLog()
"""

REWRITE_HOOK = """\
@pytest.fixture
def audit_rewrite_raw():
    def rewrite_raw(log, namespace, chain, entries):
        log._chains.setdefault(namespace, {})[chain] = copy.deepcopy(list(entries))

    return rewrite_raw
"""


def _conftest(store=PLAIN_STORE, tier="atomic", omit=()):
    """Return the conftest of an external store package, minus ``omit``."""
    fixtures = {
        "record_store_factory": (
            "@pytest.fixture\n"
            "def record_store_factory():\n"
            "    return Store()\n"
        ),
        "record_store_serves_other_namespaces": (
            "@pytest.fixture\n"
            "def record_store_serves_other_namespaces():\n"
            "    return True\n"
        ),
        "record_store_transaction_tier": (
            "@pytest.fixture\n"
            "def record_store_transaction_tier():\n"
            f"    return {tier!r}\n"
        ),
        "audit_rewrite_raw": REWRITE_HOOK,
    }
    body = "\n\n".join(text for name, text in fixtures.items() if name not in omit)
    return (
        "import copy\n\n"
        "import pytest\n\n"
        "from pipeline.store_memory import InMemoryRecordStore\n\n"
        f"{store}\n\n"
        f"{body}\n"
    )


TEST_MODULE = """\
from pipeline.testing.audit_conformance import *  # noqa: F401,F403
from pipeline.testing.outbox_conformance import *  # noqa: F401,F403
from pipeline.testing.transaction_conformance import *  # noqa: F401,F403
"""


def test_a_conforming_store_passes_all_three_published_suites(pytester):
    pytester.makeconftest(_conftest())
    pytester.makepyfile(test_plugin_conformance=TEST_MODULE)

    result = pytester.runpytest("-p", "no:cacheprovider")

    outcomes = result.parseoutcomes()
    assert outcomes.get("failed", 0) == 0
    assert outcomes.get("errors", 0) == 0
    assert outcomes.get("passed", 0) > 0
    assert result.ret == 0


def test_a_store_whose_ack_removes_more_than_the_acked_entry_fails_the_outbox_test(pytester):
    pytester.makeconftest(_conftest(store=ACK_EVERYTHING_STORE))
    pytester.makepyfile(test_plugin_conformance=TEST_MODULE)

    result = pytester.runpytest("-p", "no:cacheprovider", "-v")

    assert result.ret != 0
    result.stdout.fnmatch_lines(
        ["*test_outbox_ack_removes_exactly_the_acked_entry_and_returns_one*FAILED*"]
    )


def test_an_audit_log_that_rehashes_history_on_append_fails_the_tamper_test(pytester):
    pytester.makeconftest(_conftest(store=REHASHING_STORE))
    pytester.makepyfile(test_plugin_conformance=TEST_MODULE)

    result = pytester.runpytest("-p", "no:cacheprovider", "-v")

    assert result.ret != 0
    result.stdout.fnmatch_lines(
        ["*test_an_append_after_a_tamper_links_to_the_current_tail*FAILED*"]
    )


@pytest.mark.parametrize(
    "omitted", ["audit_rewrite_raw", "record_store_transaction_tier"]
)
def test_a_required_fixture_that_is_missing_is_an_error(pytester, omitted):
    pytester.makeconftest(_conftest(omit=(omitted,)))
    pytester.makepyfile(test_plugin_conformance=TEST_MODULE)

    result = pytester.runpytest("-p", "no:cacheprovider")

    assert result.parseoutcomes().get("errors", 0) > 0
    result.stdout.fnmatch_lines([f"*{omitted}*"])


def test_an_unknown_transaction_tier_is_an_error_that_names_the_fixture(pytester):
    pytester.makeconftest(_conftest(tier="bogus"))
    pytester.makepyfile(test_plugin_conformance=TEST_MODULE)

    result = pytester.runpytest("-p", "no:cacheprovider")

    assert result.parseoutcomes().get("errors", 0) > 0
    result.stdout.fnmatch_lines(["*record_store_transaction_tier*'bogus'*"])


@pytest.mark.parametrize("module", MODULES)
def test_importing_a_published_suite_does_not_load_the_server(module):
    probe = (
        "import sys, importlib; "
        f"importlib.import_module({module!r}); "
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
