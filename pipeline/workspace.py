"""Workspace path handling with WS-07 security hardening.

Public API
----------
* :func:`normalize_workspace_path` -- validates a caller-supplied path string
  and returns its resolved absolute :class:`~pathlib.Path`.  Raises
  :class:`WorkspaceSecurityError` (a :class:`ValueError` subclass) for every
  rejected path.
* :func:`validate_workspace` -- layers existence / git-repository / commit
  checks on top of :func:`normalize_workspace_path`; never raises.
* :func:`create_workspace` -- scaffolds a brand-new git workspace; never
  raises.
* :func:`sanitize_error_message` -- strips internal filesystem structure from
  an error string so it can safely be surfaced (e.g. as an HTTP ``detail``).

Security model (WS-07 threat model: ``POST /api/workspace`` accepts an
arbitrary path string and, with ``create: true``, runs ``mkdir`` and
``git init``)
---------------------------------------------------------------------------
This module is the PRIMARY control: every caller (HTTP route, service layer,
scripts) goes through it, so the rules are enforced here and not only at the
endpoint.

1. Structural rejection before any filesystem access: the path must be a
   non-empty absolute string with no NUL/control characters, no backslash
   separators, and no ``..`` (or dot-look-alike) segments.  The input is
   percent-decoded in a bounded loop (max 5 passes) and every decoded form is
   re-checked, so ``%2e%2e``, ``%252e%252e`` and ``..%2f`` are caught even
   though the raw spelling looks harmless.
2. Symlink policy: symlinked workspaces are NOT permitted.  If any EXISTING
   component of the caller-supplied path (final or intermediate) is a
   symbolic link, the path is rejected -- with one carve-out: system-owned
   anchor symlinks (macOS ``/tmp`` -> ``/private/tmp``, ``/var`` -> ...)
   are tolerated so legitimate locations under the system temp dir and the
   user's home keep working.  Only components the caller could have created
   are rejected.  A dangling symlink is rejected too (creating through it
   would materialize the target).
3. Sensitive-location deny list: the resolved path may not be, or live
   under, ``/etc``, ``/System``, ``/usr``, ``/bin``, ``/sbin``,
   ``/var/root``, ``/boot``, the user's ``~/.ssh``, or the pipeline's own
   repository root (:data:`REPO_ROOT`).  Matching is component-wise on the
   fully resolved paths, so ``/etcetera/ws`` and ``~/.ssh-config/ws`` stay
   allowed while every spelling of ``/etc`` (including the resolved
   ``/private/etc``) is denied.  The deny list WINS over the create
   allow-list.
4. Create allow-list: :func:`create_workspace` only creates inside
   :data:`ALLOWED_CREATE_ROOTS` (default: the system temp dir and the user's
   home).  ``validate_workspace`` is deliberately NOT allow-listed so
   existing legitimate repositories anywhere remain validatable.
5. Fail closed: every security check (symlink inspection, deny/allow root
   resolution, repo-root detection) is wrapped so that an unexpected value
   or a filesystem error results in DENIAL, never acceptance.
6. Post-create re-check: after ``mkdir``, the full validation (including the
   symlink walk and the deny list on the fresh realpath) runs again before
   ``git init``.  On failure the function reports failure and never attempts
   to unlink/rmtree the created path (a swapped symlink could make cleanup
   resolve into a sensitive location).
7. Error hygiene: raised messages and result ``error`` strings are fixed,
   generic strings -- no resolved paths, no stack traces, no internal
   filesystem structure.  :func:`sanitize_error_message` is applied to
   anything derived from the operating system or git before it is returned.
8. git init hardening: ``git init`` is always invoked with an explicit empty
   template (``--template=``) so ``init.templateDir`` / ``GIT_TEMPLATE_DIR``
   can never inject hooks, config, or credential helpers into the new repo.
   The new repo's local config also resets ``credential.helper`` to the
   empty value, which per git semantics clears any helper inherited from the
   operator's global/system gitconfig.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from urllib.parse import unquote

__all__ = [
    "ALLOWED_CREATE_ROOTS",
    "DENY_LIST_ROOTS",
    "REPO_ROOT",
    "WorkspaceSecurityError",
    "create_workspace",
    "normalize_workspace_path",
    "sanitize_error_message",
    "validate_workspace",
]

# The pipeline's own checkout.  Creating a workspace here would nest a git
# repo inside the pipeline's repo and pollute its status, so it is denied.
# Read live (module global) on every check so operators/tests can re-point it.
REPO_ROOT = Path(__file__).resolve().parent.parent


class WorkspaceSecurityError(ValueError):
    """Raised by :func:`normalize_workspace_path` for every rejected path.

    Subclasses :class:`ValueError` so pre-existing callers that catch
    ``ValueError`` keep working while still being able to distinguish
    security rejections from ordinary bad-input errors.
    """


# Sensitive locations that may never host a workspace.  Entries may be
# absolute paths, "~"-prefixed strings (expanded at check time), or
# path-like objects.  The pipeline repo root is enforced separately (live
# REPO_ROOT global) so re-pointing REPO_ROOT takes effect immediately.
DENY_LIST_ROOTS: list = [
    "/etc",
    "/System",
    "/usr",
    "/bin",
    "/sbin",
    "/var/root",
    "/boot",
    "~/.ssh",
]

# Where create_workspace may scaffold a new workspace.  Membership is
# asserted by the tests (never exact contents) so operators may extend it.
ALLOWED_CREATE_ROOTS: list = [
    tempfile.gettempdir(),
    "~",
]

# Bounded percent-decoding: enough passes to unwrap double/triple encoding
# without giving a hostile input unbounded work.
_MAX_DECODE_PASSES = 5

# Absolute-path-looking tokens inside error text (used to redact leaks).
_PATH_TOKEN_RE = re.compile(r"/[A-Za-z0-9._+~\-/]+")


# ---------------------------------------------------------------------------
# Error hygiene
# ---------------------------------------------------------------------------


def sanitize_error_message(value: object) -> str:
    """Return *value* as a string with internal filesystem structure removed.

    Stack traces, ``.py`` paths, the repo root (and its bare directory
    name), and any on-disk absolute path (e.g. a symlink target) are
    redacted.  This function never raises: even garbage input produces a
    string.
    """
    try:
        text = "" if value is None else str(value)
    except Exception:  # noqa: BLE001 - the error path itself must never raise
        return ""

    # Literal internal markers first.
    for marker in (
        "Traceback (most recent call last)",
        "Traceback",
        "site-packages",
    ):
        text = text.replace(marker, "[redacted]")

    # The pipeline's own checkout and its bare directory name.
    try:
        root_str = str(REPO_ROOT)
        if root_str:
            text = text.replace(root_str, "[redacted]")
            name = Path(root_str).name
            if name and name != root_str:
                text = text.replace(name, "[redacted]")
    except Exception:  # noqa: BLE001 - REPO_ROOT may be monkeypatched to junk
        text = "[redacted]"

    # Python source paths.
    text = text.replace(".py", "")

    # Any remaining on-disk absolute path (symlink targets, temp dirs, ...).
    def _redact(match: re.Match[str]) -> str:
        token = match.group(0)
        try:
            if os.path.exists(token) or os.path.islink(token):
                return "[path]"
        except Exception:  # noqa: BLE001 - cannot verify -> redact (fail closed)
            return "[path]"
        return token

    try:
        text = _PATH_TOKEN_RE.sub(_redact, text)
    except Exception:  # noqa: BLE001 - the error path itself must never raise
        return "[redacted error]"
    return text


# ---------------------------------------------------------------------------
# Structural validation (runs on the raw string AND every decoded form)
# ---------------------------------------------------------------------------


def _reject_unsafe_text(text: str) -> None:
    """Raise :class:`WorkspaceSecurityError` if *text* is structurally unsafe."""
    for ch in text:
        if ord(ch) < 32 or ord(ch) == 127:
            raise WorkspaceSecurityError(
                "workspace path must not contain control characters"
            )
    if "\\" in text:
        raise WorkspaceSecurityError(
            "workspace path must not contain backslash separators"
        )
    for comp in Path(text).parts:
        if comp in (".", ".."):
            raise WorkspaceSecurityError(
                "workspace path must not contain '..' segments"
            )
        # The filesystem root component ("/") is not a caller-controlled
        # segment; every real component must be separator-free.
        if comp == os.sep or comp == (os.altsep or ""):
            continue
        # Unicode look-alikes that naive decoders fold into traversal
        # segments (U+2026 HORIZONTAL ELLIPSIS -> "...", U+2025 -> "..",
        # fullwidth dots, fullwidth solidus, ...).
        folded = unicodedata.normalize("NFKC", comp)
        if "/" in folded or "\\" in folded:
            raise WorkspaceSecurityError(
                "workspace path must not contain separator look-alikes"
            )
        if folded and set(folded) == {"."} and len(folded) >= 2:
            raise WorkspaceSecurityError(
                "workspace path must not contain '..' segments"
            )


# ---------------------------------------------------------------------------
# Symlink policy
# ---------------------------------------------------------------------------


def _symlink_anchor_allowlist() -> set:
    """Spellings of system-owned anchor directories whose symlinks we tolerate.

    macOS makes ``/tmp`` -> ``/private/tmp`` and ``/var`` -> ``/private/var``;
    the system temp dir and the user's home live behind those anchors.  Only
    EXACT anchor spellings are tolerated -- a symlink anywhere the caller
    controls (e.g. under their temp workspace) is still rejected.
    """
    bases = [
        "/tmp", "/var", "/usr", "/etc", "/private", "/System", "/bin",
        "/sbin", "/Library", "/home", "/Users", "/opt", "/Applications",
        "/net", "/Volumes", "/cores", "/dev", "/sys", "/proc", "/run",
        "/boot", "/root", "/srv", "/mnt", "/media",
    ]
    try:
        bases.append(tempfile.gettempdir())
    except Exception as exc:  # noqa: BLE001 - defensive; anchors are best-effort
        print(f"workspace: tempdir anchor unavailable: {exc}")
    try:
        bases.append(os.path.expanduser("~"))
    except Exception as exc:  # noqa: BLE001 - defensive; anchors are best-effort
        print(f"workspace: home anchor unavailable: {exc}")

    anchors: set = set()
    for base in bases:
        try:
            path = Path(base)
            anchors.add(str(path))
            anchors.add(os.path.realpath(str(path)))
            for ancestor in path.parents:
                anchors.add(str(ancestor))
                anchors.add(os.path.realpath(str(ancestor)))
        except Exception as exc:  # noqa: BLE001 - defensive; anchors are best-effort
            print(f"workspace: anchor {base!r} unavailable: {exc}")
    return anchors


def _reject_caller_symlinks(abs_path: Path) -> None:
    """Reject *abs_path* if any EXISTING component is a caller-controlled symlink.

    The walk stops at the first component that does not exist yet (those
    cannot be symlinks), and tolerates system-owned anchor symlinks such as
    macOS ``/tmp``.  A dangling symlink DOES exist (lexists) and is rejected:
    creating through it would materialize the target.
    """
    try:
        anchors = _symlink_anchor_allowlist()
    except Exception as exc:
        raise WorkspaceSecurityError(
            "workspace path could not be safety-checked"
        ) from exc

    parts = abs_path.parts
    if not parts:
        raise WorkspaceSecurityError("workspace path must not be empty")

    current = Path(parts[0])
    for comp in parts[1:]:
        current = current / comp
        try:
            if not os.path.lexists(str(current)):
                break  # nothing beyond this component exists yet
            if os.path.islink(str(current)):
                spelled = str(current)
                try:
                    target = os.path.realpath(spelled)
                except Exception:  # noqa: BLE001 - unreadable link -> deny
                    target = None
                if spelled not in anchors and (
                    target is None or target not in anchors
                ):
                    raise WorkspaceSecurityError(
                        "workspace path must not traverse symbolic links"
                    )
        except OSError as exc:
            # Fail closed: an unreadable filesystem is a denial, not an OK.
            raise WorkspaceSecurityError(
                "workspace path could not be safety-checked"
            ) from exc


# ---------------------------------------------------------------------------
# Deny list / allow list (fail closed)
# ---------------------------------------------------------------------------


def _resolve_root_entry(entry: object) -> Path:
    """Resolve one deny/allow-list entry to an absolute real path."""
    if isinstance(entry, str):
        return Path(os.path.realpath(os.path.expanduser(entry)))
    if isinstance(entry, os.PathLike):
        # Duck-typed resolve so a broken entry (resolve() raising) is
        # surfaced to the caller, which fails closed.
        candidate = entry.resolve() if hasattr(entry, "resolve") else Path(entry)
        return Path(os.path.realpath(str(candidate)))
    # Malformed entry (int, None, arbitrary object) -> fail closed.
    raise WorkspaceSecurityError("workspace path could not be safety-checked")


def _is_within(candidate: Path, root: Path) -> bool:
    """Component-wise containment: *candidate* is *root* or under it."""
    return candidate == root or root in candidate.parents


def _check_deny_list(resolved_real: Path) -> None:
    """Raise if *resolved_real* is, or lives under, a protected location."""
    entries = list(DENY_LIST_ROOTS) + [REPO_ROOT]
    for entry in entries:
        try:
            root_real = _resolve_root_entry(entry)
        except WorkspaceSecurityError:
            raise
        except Exception as exc:
            # A deny root that cannot be resolved must deny, never allow.
            raise WorkspaceSecurityError(
                "workspace path could not be safety-checked"
            ) from exc
        if _is_within(resolved_real, root_real):
            raise WorkspaceSecurityError(
                "workspace path is inside a protected system location"
            )


def _check_allow_list(resolved_real: Path) -> None:
    """Raise if *resolved_real* is outside every configured create root."""
    roots = ALLOWED_CREATE_ROOTS
    if not isinstance(roots, (list, tuple)) or not roots:
        raise WorkspaceSecurityError("workspace create roots are not configured")
    matched = False
    for entry in roots:
        try:
            root_real = _resolve_root_entry(entry)
        except WorkspaceSecurityError:
            raise
        except Exception as exc:
            # A broken allow-list entry must deny, never allow.
            raise WorkspaceSecurityError(
                "workspace create root configuration is invalid"
            ) from exc
        if _is_within(resolved_real, root_real):
            matched = True
            break
    if not matched:
        raise WorkspaceSecurityError(
            "workspace path is outside the allowed create roots"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_workspace_path(raw: str | None) -> Path:
    """Return a resolved absolute :class:`Path` for *raw*.

    Enforces, in order: shape (non-empty string), structural safety on the
    raw spelling, bounded percent-decoding with structural safety re-checked
    on every decoded form, ``~`` expansion + absoluteness, the symlink walk,
    and the sensitive-location deny list.  Any unexpected error while
    checking is converted into a denial (fail closed).

    Raises
    ------
    WorkspaceSecurityError
        (a ``ValueError`` subclass) for every rejected path.
    """
    if raw is None:
        raise WorkspaceSecurityError("workspace path must be a non-empty string")
    if not isinstance(raw, str):
        raise WorkspaceSecurityError("workspace path must be a non-empty string")
    if raw.strip() == "":
        raise WorkspaceSecurityError("workspace path must not be empty")

    try:
        return _normalize_checked(raw)
    except WorkspaceSecurityError:
        raise
    except Exception as exc:
        # Fail closed: any unexpected failure during validation is a denial.
        raise WorkspaceSecurityError(
            "workspace path could not be safety-checked"
        ) from exc


def _normalize_checked(raw: str) -> Path:
    # 1. Structural checks on the raw spelling, before any decoding.
    _reject_unsafe_text(raw)

    # 2. Bounded percent-decoding; re-check every decoded form so encoded
    #    traversal (%2e%2e), double-encoded (%252e%252e) and encoded
    #    separators (..%2f) are caught.
    decoded = raw
    for _ in range(_MAX_DECODE_PASSES):
        step = unquote(decoded)
        if step == decoded:
            break
        decoded = step
        _reject_unsafe_text(decoded)

    # 3. Expand ~ and require an absolute path.
    expanded = os.path.expanduser(decoded)
    candidate = Path(expanded)
    if not candidate.is_absolute():
        raise WorkspaceSecurityError("workspace path must be absolute")
    abs_path = Path(os.path.abspath(str(candidate)))

    # 4. Symlink walk on the caller-supplied spelling (before resolution).
    _reject_caller_symlinks(abs_path)

    # 5. Sensitive-location deny list on the fully resolved path.
    resolved = Path(os.path.realpath(str(abs_path)))
    _check_deny_list(resolved)
    return resolved


def validate_workspace(raw: str | None) -> dict:
    """Validate that *raw* points to an existing git repository with commits.

    Never raises: every rejection (including security denials) becomes an
    ``ok=False`` result whose ``error`` is sanitized.
    """
    try:
        path = normalize_workspace_path(raw)
    except ValueError as exc:
        return {"ok": False, "path": "", "error": sanitize_error_message(exc)}

    try:
        if not path.exists():
            return {"ok": False, "path": str(path), "error": "path does not exist"}
        if not path.is_dir():
            return {"ok": False, "path": str(path), "error": "path is not a directory"}

        git_dir_result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-dir"],
            capture_output=True,
            check=False,
        )
        if git_dir_result.returncode != 0:
            return {"ok": False, "path": str(path), "error": "not a git repository"}

        head_result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            check=False,
        )
        if head_result.returncode != 0:
            return {
                "ok": False,
                "path": str(path),
                "error": "git repository has no commits",
            }
    except OSError as exc:
        return {"ok": False, "path": str(path), "error": sanitize_error_message(exc)}

    return {"ok": True, "path": str(path), "error": None}


def create_workspace(raw: str | None) -> dict:
    """Create a new git workspace at *raw*.

    Same result shape as :func:`validate_workspace`.  The path must pass
    :func:`normalize_workspace_path` AND the create allow-list before any
    filesystem mutation; after ``mkdir`` the safety checks run again (on the
    fresh realpath) before ``git init``.  ``git init`` always passes an
    explicit empty template so no hooks/config can be inherited, and the new
    repo's local config resets ``credential.helper`` so no helper leaks in
    from the operator's global/system gitconfig.  On any post-create failure
    the function reports failure and never deletes anything.
    """
    try:
        path = normalize_workspace_path(raw)
    except ValueError as exc:
        return {"ok": False, "path": "", "error": sanitize_error_message(exc)}

    try:
        _check_allow_list(path)
    except ValueError as exc:
        return {"ok": False, "path": "", "error": sanitize_error_message(exc)}

    try:
        if path.exists():
            if path.is_file():
                return {
                    "ok": False,
                    "path": str(path),
                    "error": "path is not a directory",
                }
            if any(path.iterdir()):
                return {"ok": False, "path": str(path), "error": "path is not empty"}
            # Existing empty directory: proceed to init.
        else:
            path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "path": str(path), "error": sanitize_error_message(exc)}

    # Post-create re-check: re-run the full validation now that the
    # components exist, so a raced symlink or a resolution drift is caught
    # before git init.  Never clean up with rmtree/unlink here: a swapped
    # symlink could make cleanup resolve into a sensitive location.
    try:
        recheck = normalize_workspace_path(raw)
    except ValueError as exc:
        return {"ok": False, "path": "", "error": sanitize_error_message(exc)}
    if recheck != path:
        return {
            "ok": False,
            "path": "",
            "error": "workspace path failed post-create safety re-check",
        }

    # Explicit empty template: overrides GIT_TEMPLATE_DIR and
    # init.templateDir, so the new repo gets no hooks or config at all.
    init_result = subprocess.run(
        ["git", "init", "--template="],
        cwd=str(path),
        capture_output=True,
        text=True,
        check=False,
    )
    if init_result.returncode != 0:
        return {
            "ok": False,
            "path": str(path),
            "error": sanitize_error_message(init_result.stderr),
        }

    # Reset the credential helper for this repo: an empty helper value
    # clears any helper inherited from the operator's global/system
    # gitconfig (git treats an empty helper value as "reset the helper
    # list"), written directly into .git/config.
    try:
        subprocess.run(
            ["git", "config", "--local", "credential.helper", ""],
            cwd=str(path),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        # Non-fatal: the repo still works; the helper reset is defense in
        # depth on top of the empty-template init.
        pass

    commit_result = subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "Initial commit"],
        cwd=str(path),
        capture_output=True,
        text=True,
        check=False,
    )
    if commit_result.returncode != 0:
        # Set a local identity so the commit succeeds even without a
        # usable global one, then retry once.
        subprocess.run(
            ["git", "config", "user.name", "pipeline"],
            cwd=str(path),
            capture_output=True,
            text=True,
            check=False,
        )
        subprocess.run(
            ["git", "config", "user.email", "pipeline@example.com"],
            cwd=str(path),
            capture_output=True,
            text=True,
            check=False,
        )
        commit_result = subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "Initial commit"],
            cwd=str(path),
            capture_output=True,
            text=True,
            check=False,
        )
        if commit_result.returncode != 0:
            return {
                "ok": False,
                "path": str(path),
                "error": sanitize_error_message(commit_result.stderr),
            }

    return {"ok": True, "path": str(path), "error": None}