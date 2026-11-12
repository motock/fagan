"""The single audit accessor is ``_audit_log()`` and returns the file-backed log.

Every audit write and read in :mod:`pipeline.audit` and
:mod:`pipeline.audit_anchor` goes through one accessor. Its name describes
what it returns, not the storage it happens to use today, so the old
``_file_log`` name must be gone.
"""

from __future__ import annotations

import pytest

from pipeline import audit
from pipeline.audit_file import FileAuditLog


@pytest.mark.parametrize("store", ["file", "sqlite"])
def test_audit_log_accessor_returns_file_audit_log_under_each_store(monkeypatch, store):
    monkeypatch.setenv("PIPELINE_STORE", store)

    assert isinstance(audit._audit_log(), FileAuditLog)


def test_file_log_accessor_name_no_longer_exists():
    assert not hasattr(audit, "_file_log")
