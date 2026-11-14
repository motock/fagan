"""The settings record kind (ADR 0015) over the SQLite store's ``settings`` table.

Two mixins, one per side of the store/view split the other kinds use:

* :class:`SettingsTablesMixin` is the host store's row layer. It holds the SQL
  and the compare-and-set, and needs the host to supply the write transaction
  helpers of :class:`pipeline.store_sqlite_records.RecordTablesMixin`.
* :class:`SettingsRecordsMixin` is the namespaced record view,
  ``SqliteRecords``. It validates the namespace and the key, then delegates.

A write reads the stored version and writes the row inside one write
transaction, so a concurrent writer cannot slip in between the check and the
store.
"""

from __future__ import annotations

import json
from typing import Any

from pipeline.store_settings import (
    next_version,
    settings_record,
    validate_settings_key,
    validate_settings_write,
)

_SETTINGS_COLUMNS = (
    'scope, scope_id, repo, values_json, allow_loosen_json, version, '
    'updated_at, updated_by'
)


def _settings_from_row(row: tuple) -> dict:
    """Return the stored settings record a ``settings`` row holds."""
    scope, scope_id, repo, values_json, allow_loosen_json, version, updated_at, updated_by = row
    return {
        'scope': scope,
        'scope_id': scope_id,
        'repo': repo,
        'values': json.loads(values_json),
        'allow_loosen': json.loads(allow_loosen_json),
        'version': version,
        'updated_at': updated_at,
        'updated_by': updated_by,
    }


class SettingsTablesMixin:
    """The settings row layer of the store, one row per settings key.

    The host supplies ``_write_records``, ``_record_row`` and ``_record_rows``
    (see ``RecordTablesMixin``) and ``_conn``.
    """

    _conn: Any

    def _settings_row(
        self, namespace: str, scope: str, scope_id: str, repo: str
    ) -> dict | None:
        """Return the settings record for the key, or ``None`` when unset."""
        row = self._record_row(
            f'SELECT {_SETTINGS_COLUMNS} FROM settings WHERE namespace = ? '
            'AND scope = ? AND scope_id = ? AND repo = ?',
            (namespace, scope, scope_id, repo),
        )
        return None if row is None else _settings_from_row(row)

    def _put_settings_row(
        self,
        namespace: str,
        scope: str,
        scope_id: str,
        repo: str,
        values: dict,
        allow_loosen: list[str],
        updated_by: str,
        expected_version: int | None,
    ) -> dict:
        """Write the settings record for the key, compare-and-set on its version.

        The read of the stored version and the write are one step inside the
        store's write transaction, so a concurrent writer cannot slip in
        between them. A conflict raises before the row is touched.
        """
        with self._write_records():
            current = self._settings_row(namespace, scope, scope_id, repo)
            record = settings_record(
                scope, scope_id, repo, values, allow_loosen,
                next_version(current, expected_version), updated_by,
            )
            self._conn.execute(
                'INSERT INTO settings (namespace, scope, scope_id, repo, '
                'values_json, allow_loosen_json, version, updated_at, updated_by) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(namespace, scope, '
                'scope_id, repo) DO UPDATE SET values_json = excluded.values_json, '
                'allow_loosen_json = excluded.allow_loosen_json, '
                'version = excluded.version, updated_at = excluded.updated_at, '
                'updated_by = excluded.updated_by',
                (
                    namespace, scope, scope_id, repo,
                    json.dumps(record['values']),
                    json.dumps(record['allow_loosen']),
                    record['version'], record['updated_at'], record['updated_by'],
                ),
            )
        return record

    def _settings_rows(self, namespace: str) -> list[dict]:
        """Return every settings record in ``namespace``, ordered by key."""
        return [
            _settings_from_row(row)
            for row in self._record_rows(
                f'SELECT {_SETTINGS_COLUMNS} FROM settings '
                'WHERE namespace = ? ORDER BY scope, scope_id, repo',
                (namespace,),
            )
        ]


class SettingsRecordsMixin:
    """The settings methods of the namespaced record view (``SqliteRecords``).

    The host view supplies ``_namespace`` (validates the namespace and refuses
    any namespace the backend does not serve) and ``_store``, the
    :class:`SettingsTablesMixin` host.
    """

    _store: Any

    def get_settings(
        self, namespace: str, scope: str, scope_id: str, repo: str
    ) -> dict | None:
        """Return the settings record for the key, or ``None`` when unset."""
        self._namespace(namespace)
        validate_settings_key(scope, scope_id, repo)
        return self._store._settings_row(namespace, scope, scope_id, repo)

    def put_settings(
        self,
        namespace: str,
        scope: str,
        scope_id: str,
        repo: str,
        values: dict,
        allow_loosen: list[str],
        updated_by: str,
        expected_version: int | None,
    ) -> dict:
        """Write the settings record for the key and return the stored record."""
        self._namespace(namespace)
        validate_settings_key(scope, scope_id, repo)
        validate_settings_write(values, allow_loosen, updated_by, expected_version)
        return self._store._put_settings_row(
            namespace, scope, scope_id, repo, values, allow_loosen,
            updated_by, expected_version,
        )

    def list_settings(self, namespace: str) -> list[dict]:
        """Return every settings record in ``namespace``, ordered by key."""
        self._namespace(namespace)
        return self._store._settings_rows(namespace)
