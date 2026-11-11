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
