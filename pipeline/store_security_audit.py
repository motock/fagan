"""The file layout of the security-audit state record (ADR 0012 decision 3).

Each repository's last security audit is one JSON file in the plans directory,
``security-audit.<repo_key>.json``. That is the name and shape the host wrote
before the record kind existed, so a state file written by an earlier build
reads back unchanged. Reads are forgiving: a missing, unreadable, malformed or
wrong-shaped file is "no audit recorded". Writes are strict and atomic.

The file-layout implementation lives here, next to the contract it satisfies,
because ``pipeline/store.py`` sits near the 1000-line gate. The host class
supplies ``_store`` (whose ``_plans_dir`` resolves the plans directory per
call) and ``_refuse_other_namespace``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from pipeline.store_records import validate_namespace, validate_repo_key

logger = logging.getLogger(__name__)


class SecurityAuditRecords:
    """The per-repo security-audit state, mixed into the file record view."""

    #: Provided by the host record view.
    _store: Any

    def _refuse_other_namespace(self, namespace: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def _audit_path(self, repo_key: str) -> Path:
        """Return the plans-directory file that holds ``repo_key``'s audit."""
        return self._store._plans_dir() / f'security-audit.{repo_key}.json'

    def get_security_audit_state(self, namespace: str, repo_key: str) -> dict | None:
        """Return the recorded audit for ``repo_key``, or ``None``.

        ``None`` means "no usable record": the file is missing, unreadable, not
        valid JSON, not a JSON object, or lacks a string ``last_audited_sha``. A
        file that exists but cannot be used is logged once at WARNING level --
        the file name only, never its contents -- and then treated as absent.
        """
        validate_namespace(namespace)
        validate_repo_key(repo_key)
        self._refuse_other_namespace(namespace)
        path = self._audit_path(repo_key)
        try:
            raw = path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return None
        except OSError:
            logger.warning('security audit state file %s could not be read', path.name)
            return None

        try:
            data = json.loads(raw)
        except ValueError:
            logger.warning('security audit state file %s is not valid JSON', path.name)
            return None

        if not isinstance(data, dict) or not isinstance(data.get('last_audited_sha'), str):
            logger.warning('security audit state file %s has an unexpected shape', path.name)
            return None
        return data

    def put_security_audit_state(
        self, namespace: str, repo_key: str, state: dict
    ) -> None:
        """Store ``state`` as ``repo_key``'s audit, replacing any previous one.

        Validation and serialisation happen before anything touches the
        plans directory, so a refused call leaves nothing behind. The write goes
        to a temp file in the same directory and is moved into place with
        ``os.replace``: a failure at any point removes the temp file and leaves
        the previous record intact.
        """
        validate_namespace(namespace)
        validate_repo_key(repo_key)
        self._refuse_other_namespace(namespace)
        if not isinstance(state, dict):
            raise TypeError('security audit state must be a dict')
        payload = json.dumps(state)

        plans = self._store._plans_dir()
        fd, tmp_name = tempfile.mkstemp(
            dir=str(plans), prefix='.security-audit-', suffix='.tmp'
        )
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(payload)
            os.replace(tmp_name, self._audit_path(repo_key))
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
