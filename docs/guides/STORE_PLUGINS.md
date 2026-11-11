# Store plugins

A store plugin is a record store implemented outside this repository, for
example a Postgres store. It is selected with `PIPELINE_STORE=plugin` and
`PIPELINE_STORE_PLUGIN=module:attr`, and it must pass the published store
contract suite before it is trusted (ADR 0012 decision 7).

See the `PIPELINE_STORE=plugin` and `PIPELINE_STORE_PLUGIN` rows in
[REFERENCE.md](../../REFERENCE.md) for the environment contract and the trust
boundary.

## Implement the store

Implement every name in `RECORD_STORE_METHODS` (`pipeline/store_records.py`).
Each method takes a `namespace` first. Core always passes `"default"`, and a
store must refuse a malformed namespace with `InvalidRecordKeyError` and a
foreign one with `UnknownNamespaceError`, unless it declares that kind as
served in a foreign namespace (see the fixture contract below).

The factory is called with no arguments and returns the store:

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

## Read the failures

A failing namespace test names the rule your store broke. For example,
`test_a_foreign_namespace_write_never_reaches_the_default_namespace[manifest]`
means a write to the foreign namespace was visible in the default namespace.
Fix the store. Do not weaken the suite.
