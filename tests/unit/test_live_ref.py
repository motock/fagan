"""Tests for ``pipeline.live_ref.LiveRef`` (the _ServerRef extraction story).

Contract under test:

- ``pipeline/live_ref.py`` exists and defines ``LiveRef`` with the exact body
  that used to live inline in ``pipeline/store.py`` as ``_ServerRef``:
  ``__init__(self, name)``, a lazy ``_value()`` that does
  ``from . import server`` at call time, and the three delegating dunders
  ``__getattr__`` / ``__call__`` / ``__truediv__``.
- The ref resolves the CURRENT ``pipeline.server`` binding at read time, never
  a snapshot taken at construction time. That laziness is the whole point of
  the indirection: the test suite patches ``pipeline.server`` for these names,
  so a module-load copy would freeze the real ~/.claude/plans path into every
  test run.
- ``pipeline.store`` no longer defines the class inline: it imports ``LiveRef``
  from the new module, keeps ``_ServerRef`` alive as an alias, leaves
  ``__all__`` untouched, and its five module-level bindings (``PLAN_DIR``,
  ``WORKTREE_ROOT``, ``_plan_lock``, ``_append_decision``, ``_append_journal``)
  keep their exact names and delegation behaviour.

Test-first note: until ``pipeline/live_ref.py`` lands, every test that touches
``LiveRef`` fails with ``ModuleNotFoundError: No module named
'pipeline.live_ref'`` -- the expected RED state for this file, not a bug in the
tests. The import is done inside each test (not at module scope) on purpose:
tests/unit/test_pytest_collection_allowlist.py asserts that a bare
``pytest --collect-only`` subprocess collects the whole tree with zero errors,
so an unimportable test module here would spill the RED state into those
unrelated allowlist tests. Function-level imports keep collection green while
this file alone stays red for the right reason.

Hermeticity: no test here reads or asserts against the real ~/.claude/plans
tree. Every ``pipeline.server`` attribute these tests touch is either a
synthetic probe name or one of the five binding names patched to a synthetic
value via ``monkeypatch``.
"""

import inspect
import re
from pathlib import Path

import pytest

from pipeline import server, store

_PROBE_PATH = "_live_ref_probe_path"
_PROBE_CALL = "_live_ref_probe_call"


def test_ref_resolves_current_value_not_construction_time(monkeypatch):
    """The ref must read pipeline.server at READ time.

    Patch a first value, construct the ref, patch a SECOND value: the ref must
    reflect the second, proving nothing was snapshotted at construction.
    """
    from pipeline.live_ref import LiveRef

    first = Path("/tmp/live-ref-first-plans")
    second = Path("/tmp/live-ref-second-plans")

    monkeypatch.setattr(server, _PROBE_PATH, first, raising=False)
    ref = LiveRef(_PROBE_PATH)

    monkeypatch.setattr(server, _PROBE_PATH, second, raising=False)

    assert (ref / "child") == second / "child"
    assert (ref / "child") != first / "child"


def test_truediv_delegates_to_the_patched_path(monkeypatch):
    from pipeline.live_ref import LiveRef

    base = Path("/tmp/live-ref-plans-root")
    monkeypatch.setattr(server, _PROBE_PATH, base, raising=False)
    ref = LiveRef(_PROBE_PATH)

    joined = ref / "child"
    assert joined == base / "child"
    assert isinstance(joined, Path)


def test_getattr_delegates_to_the_patched_path(monkeypatch):
    from pipeline.live_ref import LiveRef

    target = Path("/tmp/live-ref-plans-root/decisions.json")
    monkeypatch.setattr(server, _PROBE_PATH, target, raising=False)
    ref = LiveRef(_PROBE_PATH)

    assert ref.name == "decisions.json"
    assert ref.suffix == ".json"


def test_call_delegates_with_args_and_kwargs(monkeypatch):
    from pipeline.live_ref import LiveRef

    seen = []

    def fake_append(*args, **kwargs):
        seen.append((args, kwargs))
        return "appended-ok"

    monkeypatch.setattr(server, _PROBE_CALL, fake_append, raising=False)
    ref = LiveRef(_PROBE_CALL)

    assert ref("decision", 2, kind="journal") == "appended-ok"
    assert seen == [(("decision", 2), {"kind": "journal"})]


def test_missing_name_constructs_lazily_then_raises_attribute_error():
    """Construction must NOT look the name up (lazy); the first attribute read
    is what raises AttributeError, with the missing name in the message."""
    from pipeline.live_ref import LiveRef

    ref = LiveRef("NOT_A_REAL_NAME")

    with pytest.raises(AttributeError) as excinfo:
        _ = ref.anything  # attribute read IS the trigger
    assert "NOT_A_REAL_NAME" in str(excinfo.value)

    with pytest.raises(AttributeError):
        ref()


def test_store_serverref_alias_survives_as_live_ref():
    from pipeline import live_ref

    assert store._ServerRef is live_ref.LiveRef
    assert store.LiveRef is live_ref.LiveRef


def test_store_no_longer_defines_the_class_inline():
    src = inspect.getsource(store)

    # Mirrors the mechanical criterion: grep -c 'class _ServerRef' == 0.
    assert "class _ServerRef" not in src
    # The class now comes from the new module (absolute or relative import).
    assert re.search(r"from (pipeline\.live_ref|\.live_ref) import LiveRef", src) is not None
    # The alias assignment the brief mandates.
    assert re.search(r"_ServerRef\s*=\s*(LiveRef|live_ref\.LiveRef)", src) is not None


def test_store_module_bindings_keep_names_and_behaviour(monkeypatch):
    plans = Path("/tmp/live-ref-store-plans")
    worktree = Path("/tmp/live-ref-store-worktree")

    class FakeLock:
        def __init__(self):
            self.acquired = False

        def acquire(self):
            self.acquired = True
            return True

    lock = FakeLock()

    def fake_append_decision(entry):
        return f"decision:{entry}"

    monkeypatch.setattr(server, "PLAN_DIR", plans, raising=False)
    monkeypatch.setattr(server, "WORKTREE_ROOT", worktree, raising=False)
    monkeypatch.setattr(server, "_plan_lock", lock, raising=False)
    monkeypatch.setattr(server, "_append_decision", fake_append_decision, raising=False)
    monkeypatch.setattr(
        server, "_append_journal", lambda entry: f"journal:{entry}", raising=False
    )

    assert (store.PLAN_DIR / "plan.md") == plans / "plan.md"
    assert store.WORKTREE_ROOT.name == "live-ref-store-worktree"
    assert store._plan_lock.acquire() is True
    assert lock.acquired is True
    assert store._append_decision("e1") == "decision:e1"
    assert store._append_journal("e2") == "journal:e2"


def test_live_ref_module_docstring_explains_why_the_indirection_exists():
    from pipeline import live_ref

    doc = live_ref.__doc__ or ""
    assert "pipeline.server" in doc
    assert "patch" in doc.lower()
    assert "freeze" in doc.lower()


def test_live_ref_defines_the_four_delegating_methods_with_lazy_server_import():
    from pipeline.live_ref import LiveRef

    for name in ("__getattr__", "__call__", "__truediv__", "_value"):
        assert callable(getattr(LiveRef, name, None)), f"LiveRef.{name} missing"

    # _value must import pipeline.server lazily (inside the method), not at
    # module load -- that is what keeps monkeypatch.setattr(pipeline.server,
    # ...) landing.
    value_src = inspect.getsource(LiveRef._value)
    assert re.search(r"\bimport\b", value_src) is not None
    assert "server" in value_src


def test_store_all_is_untouched():
    assert store.__all__ == ["FileStore", "Store", "_TransactionLock"]