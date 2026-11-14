"""The settings record kind (ADR 0015) for the file layout.

Records are keyed (scope, scope_id, repo) inside one namespace. The
in-memory store reuses the validators and the version rule from here so the
two stores cannot disagree about a key or a conflict. This module is kept
apart from ``store_records`` to hold that file under the 1000-line gate.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pipeline.store_records import (
    InvalidRecordKeyError,
    RecordStoreError,
    UnreadableRecordError,
    validate_namespace,
)

SETTINGS_SCOPES = ('deployment', 'org', 'team', 'team_repo')
REPO_MAX_LENGTH = 256

# Same shape as ``app/credentials.py``'s identifier: a lower-case name a
# config file can hold without quoting. Matched with ``fullmatch`` so a
# trailing newline cannot slip through a ``$`` anchor.
_IDENTIFIER_RE = re.compile(r'[a-z][a-z0-9_.-]{0,63}')
# ``host/org/name``: the host may carry a port, the org and name are the
# characters forge slugs use.
_REPO_RE = re.compile(r'[A-Za-z0-9.:-]+/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+')

_RECORD_PREFIX = 'settings-record-'
_RECORD_NAME_RE = re.compile(r'settings-record-[0-9a-f]{64}\.json')
_LOCK_NAME = '.settings-record.lock'


class SettingsVersionConflictError(RecordStoreError):
    """Raised when a settings write's ``expected_version`` does not match.

    A create (``expected_version=None``) of a record that already exists, and a
    write whose expected version is stale or names a missing record, both raise
    this. The stored record is left unchanged. It lives here, beside the
    settings rules, so ``store_records`` stays under the line gate.
    """


def validate_settings_key(scope: object, scope_id: object, repo: object) -> None:
    """Refuse a settings key whose scope, scope id or repo has the wrong shape.

    Raises :class:`InvalidRecordKeyError` before anything is read or written.
    """
    if scope not in SETTINGS_SCOPES:
        raise InvalidRecordKeyError('invalid settings scope')
    if not isinstance(scope_id, str) or not isinstance(repo, str):
        raise InvalidRecordKeyError('invalid settings key')

    if scope == 'deployment':
        if scope_id:
            raise InvalidRecordKeyError('a deployment settings record has no scope id')
    elif _IDENTIFIER_RE.fullmatch(scope_id) is None:
        raise InvalidRecordKeyError('invalid settings scope id')

    if scope == 'team_repo':
        _validate_repo(repo)
    elif repo:
        raise InvalidRecordKeyError('only a team_repo settings record has a repo')


def _validate_repo(repo: str) -> None:
    if len(repo) > REPO_MAX_LENGTH:
        raise InvalidRecordKeyError('settings repo is too long')
    if any(ord(char) < 32 or ord(char) == 127 for char in repo):
        raise InvalidRecordKeyError('settings repo contains a control character')
    if _REPO_RE.fullmatch(repo) is None:
        raise InvalidRecordKeyError('invalid settings repo')


def validate_settings_write(
    values: object,
    allow_loosen: object,
    updated_by: object,
    expected_version: object,
) -> None:
    """Refuse a settings write whose value, flags or version has the wrong type.

    Which keys ``values`` may hold is the resolver's decision, not the store's.
    The value set must be JSON-serialisable, so a refused write stores nothing.
    """
    if not isinstance(values, dict) or not all(isinstance(key, str) for key in values):
        raise TypeError('settings values must be a dict keyed by strings')
    if not isinstance(allow_loosen, list) or not all(
        isinstance(key, str) for key in allow_loosen
    ):
        raise TypeError('settings allow_loosen must be a list of key names')
    if not isinstance(updated_by, str):
        raise TypeError('settings updated_by must be a str')
    if expected_version is not None and (
        isinstance(expected_version, bool) or not isinstance(expected_version, int)
    ):
        raise TypeError('settings expected_version must be an int or None')
    json.dumps(values)


def next_version(current: dict | None, expected_version: int | None) -> int:
    """Return the version a write takes, or refuse it as a version conflict.

    ``current`` is the stored record or ``None``. A create (``expected_version``
    of ``None``) needs no record; any other write needs the stored version to
    equal ``expected_version``.
    """
    if expected_version is None:
        if current is not None:
            raise SettingsVersionConflictError('settings record already exists')
        return 1
    if current is None or current['version'] != expected_version:
        raise SettingsVersionConflictError('settings record version does not match')
    return current['version'] + 1


def settings_record(
    scope: str,
    scope_id: str,
    repo: str,
    values: dict,
    allow_loosen: list[str],
    version: int,
    updated_by: str,
) -> dict:
    """Build the stored form of one settings record, stamped with the time now."""
    return {
        'scope': scope,
        'scope_id': scope_id,
        'repo': repo,
        'values': copy.deepcopy(values),
        'allow_loosen': sorted(set(allow_loosen)),
        'version': version,
        'updated_at': datetime.now(UTC).isoformat(),
        'updated_by': updated_by,
    }


def _record_name(scope: str, scope_id: str, repo: str) -> str:
    key = json.dumps([scope, scope_id, repo])
    return f'{_RECORD_PREFIX}{hashlib.sha256(key.encode("utf-8")).hexdigest()}.json'


def _decode_record(raw: str, name: str) -> dict:
    """Parse one stored record file, or raise :class:`UnreadableRecordError`.

    The error names the file only, never its contents: a settings value is
    configuration, and a refusal must not echo it.
    """
    try:
        record = json.loads(raw)
    except ValueError as exc:
        raise UnreadableRecordError(f'settings record file {name} is not valid JSON') from exc

    if not isinstance(record, dict) or not _has_record_shape(record):
        raise UnreadableRecordError(f'settings record file {name} has an unexpected shape')
    if name != _record_name(record['scope'], record['scope_id'], record['repo']):
        raise UnreadableRecordError(f'settings record file {name} holds a different key')
    return record


def _has_record_shape(record: dict) -> bool:
    return (
        all(isinstance(record.get(field), str) for field in ('scope', 'scope_id', 'repo', 'updated_at', 'updated_by'))
        and isinstance(record.get('values'), dict)
        and isinstance(record.get('allow_loosen'), list)
        and isinstance(record.get('version'), int)
        and not isinstance(record.get('version'), bool)
    )


def _read_record_file(path: Path) -> dict | None:
    """Return the record stored at ``path``, or ``None`` when there is none."""
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    return _decode_record(raw, path.name)


@contextlib.contextmanager
def _settings_lock(plans: Path) -> Iterator[None]:
    """Serialise read-check-write across processes with an exclusive flock.

    The lock file is the plans directory's, and it is never a record: the
    record glob does not match it.
    """
    with open(plans / _LOCK_NAME, 'a', encoding='utf-8') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


class SettingsRecords:
    """The file-layout implementation of the settings record kind.

    Mixed into the file record view. The host supplies ``_store`` (whose
    ``_plans_dir`` resolves the plans directory per call) and
    ``_refuse_other_namespace``.
    """

    #: Provided by the host record view.
    _store: Any

    def _refuse_other_namespace(self, namespace: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def _settings_dir(self) -> Path:
        return self._store._plans_dir()

    def get_settings(
        self, namespace: str, scope: str, scope_id: str, repo: str
    ) -> dict | None:
        """Return the settings record for the key, or ``None`` when it is unset."""
        validate_namespace(namespace)
        self._refuse_other_namespace(namespace)
        validate_settings_key(scope, scope_id, repo)
        return _read_record_file(
            self._settings_dir() / _record_name(scope, scope_id, repo)
        )

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
        """Write the settings record for the key and return the stored record.

        Refused before the plans directory is touched if the key or the write is
        malformed. A version conflict leaves the previous record in place. The
        write goes to a temp file in the same directory and is moved into place
        with ``os.replace``.
        """
        validate_namespace(namespace)
        self._refuse_other_namespace(namespace)
        validate_settings_key(scope, scope_id, repo)
        validate_settings_write(values, allow_loosen, updated_by, expected_version)

        plans = self._settings_dir()
        path = plans / _record_name(scope, scope_id, repo)
        with _settings_lock(plans):
            current = _read_record_file(path)
            version = next_version(current, expected_version)
            record = settings_record(
                scope, scope_id, repo, values, allow_loosen, version, updated_by
            )
            _write_record_file(plans, path, record)
        return record

    def list_settings(self, namespace: str) -> list[dict]:
        """Return every settings record in ``namespace``, ordered by key."""
        validate_namespace(namespace)
        self._refuse_other_namespace(namespace)
        records = []
        for path in sorted(self._settings_dir().glob(f'{_RECORD_PREFIX}*.json')):
            if _RECORD_NAME_RE.fullmatch(path.name) is None:
                continue
            records.append(_decode_record(path.read_text(encoding='utf-8'), path.name))
        return sorted(records, key=lambda record: (record['scope'], record['scope_id'], record['repo']))


def _write_record_file(plans: Path, path: Path, record: dict) -> None:
    payload = json.dumps(record)
    fd, tmp_name = tempfile.mkstemp(dir=str(plans), prefix='.settings-record-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
