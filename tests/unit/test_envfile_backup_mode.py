"""Regression tests: env-file backups must keep the source file's mode.

``pipeline.setup.envfile`` promises 0600 for secret-bearing env files. The
backup taken before an update is a byte-for-byte copy of the previous file, so
it carries the same secrets and must carry the same mode. ``shutil.copyfile``
does not copy the mode, so a 0600 secret file used to leave a world-readable
0644 ``.bak.<stamp>`` sibling behind — a secret leak.

These tests pin the mode of the backup to the mode of the file it copies, and
deliberately use a non-default source mode (0640) so the assertion cannot pass
by accident through the process umask.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from pipeline.setup import envfile

_NOW = datetime(2026, 10, 22, 12, 34, 56, tzinfo=UTC)


def _mode(path: str | Path) -> int:
    """Return the permission bits of ``path``."""
    return stat.S_IMODE(Path(path).stat().st_mode)


def _seed(path: Path, text: str, mode: int) -> None:
    """Create ``path`` with ``text`` and an explicit ``mode``."""
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)


def test_backup_of_secret_env_file_stays_0600(tmp_path):
    """A 0600 secret-bearing env file must not leave a 0644 backup."""
    target = tmp_path / ".pipeline.env"
    _seed(target, "API_KEY='old-secret'\n", 0o600)

    result = envfile.write_env_file(
        target, {"API_KEY": "new-secret"}, secret_keys=("API_KEY",), now=lambda: _NOW
    )

    backup = Path(str(result["backup"]))
    assert backup.exists()
    assert backup.read_bytes() == b"API_KEY='old-secret'\n"
    assert _mode(backup) == 0o600


def test_backup_preserves_a_non_default_source_mode(tmp_path):
    """The backup copies the source mode, not the umask-derived default."""
    target = tmp_path / ".pipeline.env"
    _seed(target, "API_KEY='old-secret'\n", 0o640)

    result = envfile.write_env_file(
        target, {"API_KEY": "new-secret"}, secret_keys=("API_KEY",), now=lambda: _NOW
    )

    assert _mode(Path(str(result["backup"]))) == 0o640


def test_backup_of_public_env_file_stays_0644(tmp_path):
    """A non-secret env file keeps its 0644 mode on the backup too."""
    target = tmp_path / ".pipeline.env"
    _seed(target, "LOG_LEVEL='info'\n", 0o644)

    result = envfile.write_env_file(target, {"LOG_LEVEL": "debug"}, now=lambda: _NOW)

    assert _mode(Path(str(result["backup"]))) == 0o644
