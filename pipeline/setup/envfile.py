"""Read, render and atomically write the guided-setup env file.

Three public functions (see ``__all__``):

``read_env_file``
    Delegates to :func:`pipeline.env_file.parse_env_file`, so the parsing
    rules live in exactly one place. A missing file, a directory path or
    undecodable bytes read as an empty mapping.

``render_env_text``
    Turns a mapping into shell-sourceable text: one ``KEY='value'`` line per
    entry in insertion order, each line ending with a newline. Single quotes
    keep ``$``, backticks, spaces, ``#``, ``=`` and ``~`` literal when the
    file is sourced; an embedded single quote is encoded as the standard
    close/escape/reopen sequence ``'\\''``.

``write_env_file``
    Renders first (a validation error touches nothing), treats a file whose
    bytes already match as a no-op, backs up a differing file next to it
    under a UTC timestamped name that is never reused, and installs the new
    bytes through a same-directory temp file that is fsynced and
    ``os.replace``d, so no code path can leave a partial file behind. The
    file gets mode 0600 when a secret key holds a non-empty value, else 0644.

The module is pure: it imports no logging, prints nothing, and never reads
or writes ``os.environ``. Error messages name the offending key only, never
its value, so a secret cannot leak through an exception.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from pipeline.env_file import parse_env_file

__all__ = ["read_env_file", "render_env_text", "write_env_file"]

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SECRET_MODE = 0o600
_PUBLIC_MODE = 0o644
_BACKUP_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def read_env_file(path: str | Path) -> dict[str, str]:
    """Return the variables defined in ``path``.

    Delegates to :func:`pipeline.env_file.parse_env_file`: a missing file, a
    directory path or undecodable bytes read as an empty mapping, and the
    process environment is never consulted or modified.
    """
    return parse_env_file(path)


def render_env_text(values: Mapping[str, str]) -> str:
    """Render ``values`` as shell-sourceable text.

    One ``KEY='value'`` line per entry, in the mapping's insertion order,
    each line ending with a newline (an empty mapping renders as ``""``).
    Single quotes keep ``$``, backticks, spaces, ``#``, ``=`` and ``~``
    literal when the file is sourced; an embedded single quote is encoded as
    the standard close/escape/reopen sequence ``'\\''``.

    Raises ValueError, naming the offending key but never its value, when a
    key is not a valid environment identifier or a value contains a newline
    or carriage return.
    """
    lines: list[str] = []
    for raw_key, raw_value in values.items():
        key = _validated_key(raw_key)
        value = _validated_value(key, raw_value)
        escaped = value.replace("'", "'\\''")
        lines.append(f"{key}='{escaped}'")
    return "".join(f"{line}\n" for line in lines)


def write_env_file(
    path: str | Path,
    values: Mapping[str, str],
    *,
    secret_keys: Iterable[str] = (),
    now: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    """Write ``values`` to ``path`` atomically, backing up what was there.

    Rendering happens first, so a validation error leaves ``path`` and any
    existing backup untouched. If ``path`` already holds exactly the
    rendered bytes, nothing is written and no backup is made. Otherwise the
    current bytes are copied verbatim to ``<name>.bak.<UTC stamp>`` next to
    ``path`` first (appending ``.1``, ``.2``, ... on a same-stamp collision;
    an existing backup is never overwritten), and the new content then
    replaces the file through a same-directory temp file that is fsynced,
    chmodded and ``os.replace``d.

    The file is mode 0600 when any key in ``secret_keys`` is present in
    ``values`` with a non-empty value, else 0644. ``now`` supplies the backup
    timestamp and defaults to ``datetime.now(timezone.utc)``.

    Returns ``{"changed": bool, "backup": Path | None, "mode": "0600" |
    "0644", "path": str}``.
    """
    target = Path(path)
    data = render_env_text(values).encode("utf-8")
    mode = _SECRET_MODE if _has_secret(values, secret_keys) else _PUBLIC_MODE

    backup: Path | None = None
    if target.exists():
        if target.read_bytes() == data:
            return {
                "changed": False,
                "backup": None,
                "mode": _mode_name(mode),
                "path": str(target),
            }
        backup = _backup_existing(target, _timestamp(now))

    _replace_atomically(target, data, mode)
    return {
        "changed": True,
        "backup": backup,
        "mode": _mode_name(mode),
        "path": str(target),
    }


def _validated_key(key: object) -> str:
    """Return ``key`` when it is a valid environment identifier, else raise."""
    if not isinstance(key, str) or _KEY_RE.fullmatch(key) is None:
        raise ValueError(f"invalid environment key: {key!r}")
    return key


def _validated_value(key: str, value: object) -> str:
    """Return ``value`` when it can be rendered safely, else raise.

    The error names ``key`` only; the value itself is never echoed, so a
    secret cannot leak through an exception message.
    """
    if not isinstance(value, str):
        raise TypeError(f"value for environment key {key!r} is not a string")
    if "\n" in value or "\r" in value:
        raise ValueError(
            f"value for environment key {key!r} contains a newline or carriage return"
        )
    return value


def _has_secret(values: Mapping[str, str], secret_keys: Iterable[str]) -> bool:
    """True when a secret key is present in ``values`` with a non-empty value."""
    return any(values.get(key, "") != "" for key in secret_keys)


def _timestamp(now: Callable[[], datetime] | None) -> str:
    """Format the backup stamp: UTC ``YYYYMMDDTHHMMSSZ``."""
    moment = datetime.now(UTC) if now is None else now()
    return moment.strftime(_BACKUP_STAMP_FORMAT)


def _backup_existing(target: Path, stamp: str) -> str:
    """Copy ``target`` verbatim to a fresh ``.bak.<stamp>`` sibling.

    A name that already exists is never reused: the first collision gets
    ``.1`` appended, the next ``.2``, and so on. Returns the backup path as
    a string.

    ``shutil.copy`` rather than ``shutil.copyfile``: the backup holds the same
    bytes as ``target``, so it must hold the same permission bits too. A
    ``copyfile`` backup is created with the umask-derived default (0644), which
    would turn a 0600 secret-bearing env file into a world-readable backup.
    """
    base = f"{target.name}.bak.{stamp}"
    candidate = target.with_name(base)
    suffix = 0
    while candidate.exists():
        suffix += 1
        candidate = target.with_name(f"{base}.{suffix}")
    shutil.copy(target, candidate)
    return str(candidate)


def _replace_atomically(target: Path, data: bytes, mode: int) -> None:
    """Install ``data`` at ``target`` without ever leaving a partial file.

    The bytes go to a temp file in the same directory, are fsynced, get the
    final mode, and are then moved onto the target with ``os.replace``. Any
    failure removes the temp file and propagates, leaving the original
    intact.
    """
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{target.name}.tmp", dir=str(target.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _mode_name(mode: int) -> str:
    """Render a mode as the ``0600``-style string the result reports."""
    return f"{mode:04o}"