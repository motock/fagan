"""Implementation of global rules bundle installation.

This module provides the public API required by the tests for the
``GR-4`` story.  It is intentionally self‑contained and imports only
``pipeline.global_rules_targets`` and ``pipeline.managed_block``.

The public symbols are:

* :class:`InstallResult` – a frozen dataclass describing the result of an
  installation.
* :func:`render_block` – renders the two source files and appends a line
  containing the absolute ``rules_dir``.
* :func:`install_for_tool` – orchestrates copying the rule files, applying the
  managed block to the target file, handling backups, warnings and dry‑run
  semantics.
* :func:`_write_atomic` – helper that writes a file atomically.

The implementation follows the behaviour exercised by the unit tests.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import global_rules_targets
from .managed_block import apply_managed_block


@dataclass(frozen=True)
class InstallResult:
    """Result of :func:`install_for_tool`.

    Attributes
    ----------
    tool:
        The tool name.
    target:
        Path to the instruction file that was (or would be) written.
    changed:
        ``True`` if the target file was modified.
    backup:
        Path to a backup file created when the target existed before the
        write, or ``None``.
    warning:
        One‑line warning string if a shadowing file exists, otherwise
        ``None``.
    """

    tool: str
    target: Path
    changed: bool
    backup: Path | None
    warning: str | None


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _write_atomic(path: Path, text: str) -> None:
    """Write *text* to *path* atomically.

    The function creates a temporary file in the same directory as ``path`` and
    then replaces ``path`` with the temporary file using :func:`os.replace`.
    The temporary file is removed regardless of success.
    """
    tmp_dir = path.parent
    fd, tmp_path = os.mkstemp(dir=str(tmp_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, path)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def render_block(source_root: Path, rules_dir: Path) -> str:
    """Render the global rules block.

    The block consists of the contents of ``<source_root>/global-rules/standards.md``
    followed by the contents of ``<source_root>/global-rules/pipeline-workflow.md``
    and a final line containing the absolute ``rules_dir``.

    Parameters
    ----------
    source_root:
        The root directory containing the ``global-rules`` directory.
    rules_dir:
        The directory that will contain the rule files.

    Returns
    -------
    str
        The concatenated block.

    Raises
    ------
    FileNotFoundError
        If either of the source files is missing.
    """
    std_path = source_root / "global-rules" / "standards.md"
    wf_path = source_root / "global-rules" / "pipeline-workflow.md"
    std_text = std_path.read_text(encoding="utf-8")
    wf_text = wf_path.read_text(encoding="utf-8")
    # Append a final line with the absolute rules_dir.
    final_line = str(rules_dir)
    return f"{std_text}{wf_text}{final_line}\n"


def install_for_tool(tool: str, source_root: Path, env: dict[str, str], *, dry_run: bool) -> InstallResult:
    """Install the global rules bundle for *tool*.

    Parameters
    ----------
    tool:
        One of the supported tools.
    source_root:
        Root directory containing the source files.
    env:
        Environment mapping used by :mod:`pipeline.global_rules_targets`.
    dry_run:
        If ``True`` the function performs all calculations but does not
        modify the filesystem.

    Returns
    -------
    InstallResult
        Result describing what happened.

    Raises
    ------
    ValueError
        If the target or rules directory is a symlink, if the tool is
        unsupported, or if the target file contains malformed markers.
    FileNotFoundError
        If a required source file is missing.
    """
    # Resolve target and rules directory.
    target = global_rules_targets.instructions_path(tool, env)
    rules_dir = global_rules_targets.rules_dir(tool, env)

    # Fail closed on symlinks.
    if target.is_symlink():
        raise ValueError(f"Target {target} is a symlink")
    if rules_dir.is_symlink():
        raise ValueError(f"Rules dir {rules_dir} is a symlink")

    # Compute warning before any potential early return.
    shadow = global_rules_targets.shadowing_path(tool, env)
    warning: str | None = None
    if shadow is not None and shadow.exists():
        warning = f"{tool}: {shadow} exists and takes precedence, so this bundle will not be read"

    # Copy rule files unless dry_run.
    if not dry_run:
        rules_dir.mkdir(parents=True, exist_ok=True)
        src_rules = source_root / ".claude" / "rules"
        for src_file in src_rules.glob("*.md"):
            dst_file = rules_dir / src_file.name
            shutil.copy2(src_file, dst_file)

    # Read existing target if present.
    existing = ""
    if target.exists():
        existing = target.read_text(encoding="utf-8")

    # Render block and apply managed block.
    block = render_block(source_root, rules_dir)
    try:
        new_text = apply_managed_block(existing, block)
    except ValueError:
        # Propagate marker errors without modifying the file.
        raise

    # Determine if anything changed.
    if new_text == existing:
        return InstallResult(tool=tool, target=target, changed=False, backup=None, warning=warning)

    # At this point we will write the file.
    backup: Path | None = None
    if not dry_run:
        # Backup if target existed.
        if target.exists():
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = target.with_name(f"{target.name}.fagan-bak-{timestamp}")
            shutil.copy2(target, backup)
        # Ensure parent directory exists.
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(target, new_text)

    return InstallResult(tool=tool, target=target, changed=True, backup=backup, warning=warning)


# End of module
