# Store plugins

A store plugin is a record store implemented outside this repository, for
example a Postgres store. It is selected with `PIPELINE_STORE=plugin` and
`PIPELINE_STORE_PLUGIN=module:attr`, and it must pass the published store
contract suites before it is trusted (ADR 0012 decision 7): the store suite,
plus the outbox, audit and transaction suites described below.

See the `PIPELINE_STORE=plugin` and `PIPELINE_STORE_PLUGIN` rows in
[REFERENCE.md](../../REFERENCE.md) for the environment contract and the trust
boundary.

## Implement the store

Implement every name in `PLUGIN_STORE_METHODS` (`pipeline/store_plugin.py`).
That is the record surface, `RECORD_STORE_METHODS` (`pipeline/store_records.py`),
plus the outbox methods (`append_outbox`, `read_outbox`, `ack_outbox`,
`list_outboxes`, `outbox_summary`) and `audit_log()`. Each record method takes a
`namespace` first. Core always passes `"default"`, and a store must refuse a
malformed namespace with `InvalidRecordKeyError` and a foreign one with
`UnknownNamespaceError`, unless it declares that kind as served in a foreign
namespace (see the fixture contract below).

The factory is called with no arguments and returns the store. The plugin
loader wraps that result in `PluginStore`, which serves the server's legacy
`Store` calls over your namespaced methods, so you implement the namespaced
surface only. A store missing any name in `PLUGIN_STORE_METHODS` is refused at
startup, and the refusal names the missing methods.

```python
# mystore/store.py
class PostgresRecordStore:
    ...


def build_store():
    return PostgresRecordStore(dsn=...)
```

Select it with:

```bash
export PIPELINE_STORE=plugin
export PIPELINE_STORE_PLUGIN=mystore.store:build_store
```

## Run the conformance gate in CI

The suite ships inside this package as `pipeline.testing.store_conformance`. It
needs no `tests/` tree from this repository. Your package's conftest supplies
the `record_store_factory` fixture, a fresh and empty store for namespace
`default`:

```python
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
```

Replace `InMemoryRecordStore()` with your store and declare its policy in the two
optional fixtures below. Then collect the suite from a
test module, because pytest collects tests only from test modules and not from
conftest files:

```python
from pipeline.testing.store_conformance import *  # noqa: F401,F403
```

Run it in your CI like any other test suite:

```bash
pytest -q tests/test_store_conformance.py
```

### Optional fixtures

The suite reads two more fixtures. Override them only when your store differs
from the default.

- `record_store_serves_other`: a frozenset of record-kind names your store
  serves in a foreign namespace. The default is none, which means every foreign
  namespace is refused. The kind names are `manifest`, `decisions`, `journal`,
  `usage`, `notification`, `plan_draft`, `completion`, `plan_report` and
  `context_snapshot`.
- `record_store_known_gaps`: a dict of strict-xfail reasons keyed by check name.
  Use it only for a defect you have recorded, because an unexpected pass fails
  the run.

## Run the outbox, audit and transaction suites

The plugin surface has three more published suites. Collect each one from a
test module the same way, next to the store suite:

```python
from pipeline.testing.outbox_conformance import *  # noqa: F401,F403
from pipeline.testing.audit_conformance import *  # noqa: F401,F403
from pipeline.testing.transaction_conformance import *  # noqa: F401,F403
```

They read three fixtures that the store suite does not. Add them to the same
conftest as `record_store_factory`:

```python
import pytest


@pytest.fixture
def audit_rewrite_raw():
    # Required by the audit suite. Replace the stored entries of one chain
    # verbatim, bypassing the protocol, so the suite can prove tamper detection.
    def rewrite(log, namespace, chain, entries):
        ...

    return rewrite


@pytest.fixture
def record_store_transaction_tier():
    # Required by the transaction suite: "atomic" if the outermost
    # transaction() block commits or rolls back every write it made, or
    # "ordered" if each write is durable when its call returns.
    return "atomic"


@pytest.fixture
def record_store_serves_other_namespaces():
    # Optional, read by the outbox, audit and transaction suites. True for a
    # store that serves every valid namespace; absent or False takes the
    # strict reading, where a foreign namespace is refused.
    return False
```

- `audit_rewrite_raw` is required. A callable `(log, namespace, chain, entries)`
  that replaces the stored entries of one chain. A store that cannot provide one
  cannot claim tamper detection, so a missing fixture is an error, not a skip.
- `record_store_transaction_tier` is required. Any value other than `"atomic"`
  or `"ordered"` is an error.
- `record_store_serves_other_namespaces` is optional. It is the same policy as
  the store suite's `record_store_serves_other` frozenset, but with a different
  name: the outbox, audit and transaction suites read a boolean under this name,
  and the store suite reads a frozenset under the shorter one. Set both to match
  your store.

The transaction suite also reads `record_store_plans_dir`, optional, for a
file-backed store: the directory it locks in. Given it, the suite checks that a
rejected key takes no lock file.

## Read the failures

A failing namespace test names the rule your store broke. For example,
`test_a_foreign_namespace_write_never_reaches_the_default_namespace[manifest]`
means a write to the foreign namespace was visible in the default namespace.
Fix the store. Do not weaken the suite.
