"""Store plugin factories loaded by tests/unit/test_store_plugin.py.

``build_record_store`` returns a conforming record store; ``build_incomplete``
returns one that lacks most of ``RECORD_STORE_METHODS``. Both are defined here
(not re-exported) so the loader's owner check accepts them.
"""

from pipeline.store_memory import InMemoryRecordStore


def build_record_store():
    return InMemoryRecordStore()


class _IncompleteStore:
    def get_manifest(self, namespace, plan_name):
        return None

    def put_manifest(self, namespace, plan_name, manifest):
        return None


def build_incomplete():
    return _IncompleteStore()


_SHARED: dict[str, InMemoryRecordStore] = {}


def build_shared_record_store():
    """Return the one in-memory store this process shares across calls.

    A plugin's records live outside the process, so every ``build_store()``
    call reaches the same data. :func:`build_record_store` builds a fresh
    store per call, which would hide a manifest written by an earlier call.
    """
    if "store" not in _SHARED:
        _SHARED["store"] = InMemoryRecordStore()
    return _SHARED["store"]


def reset_shared_record_store():
    """Forget the shared store, so the next test starts from empty."""
    _SHARED.clear()


class _AuditRefusingStore(InMemoryRecordStore):
    def audit_log(self):
        raise RuntimeError("audit log unavailable")


def build_audit_refusing_record_store():
    """A conforming store whose audit log cannot be opened."""
    return _AuditRefusingStore()
