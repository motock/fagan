"""WS-07 security tests for the workspace HTTP surface (WS-05/WS-06).

Threat model: ``POST /api/workspace`` accepts an arbitrary path string and,
with ``create: true``, runs ``mkdir`` and ``git init``. The hardening lives in
``pipeline/workspace.py`` (the module is the primary control so EVERY caller
is protected; the HTTP route is defense in depth).

Contract pinned here (all currently missing -> RED until implemented):

1. ``WorkspaceSecurityError`` — a ``ValueError`` subclass raised by
   ``normalize_workspace_path`` for every rejected path, so callers can
   distinguish security rejections from ordinary bad-input errors.
2. Path traversal — ``..`` segments in any encoding (raw, percent-encoded,
   backslash, NUL, unicode look-alikes) are rejected before resolution.
3. Symlinks — a workspace whose final OR intermediate component is a symlink
   is rejected (symlinked workspaces are NOT permitted). System anchor
   symlinks on the way to a legitimate location (macOS ``/tmp`` ->
   ``/private/tmp``) are tolerated, which is what keeps the tmp_path
   positive tests green.
4. Sensitive-location deny list — creating/validating inside ``/etc``,
   ``/System``, the user's ``.ssh``, or the pipeline's own repo root is
   denied. The deny list WINS over any allow-list. Checks fail CLOSED:
   a malformed deny-list entry or an error while inspecting the path
   results in denial, never acceptance.
5. Allow-list — ``create_workspace`` only creates inside configured roots
   (default: the user's home and the system temp dir). ``ALLOWED_CREATE_ROOTS``
   and ``DENY_LIST_ROOTS`` are module-level, operator-extensible lists
   (asserted by membership, never exact contents, so later stories can
   extend them).
6. Error hygiene — ``sanitize_error_message`` strips internal filesystem
   structure (repo root, ``.py`` paths, ``Traceback``, symlink targets) from
   messages; ``create_workspace``/``validate_workspace`` results never leak
   them, and the HTTP ``detail`` mirrors that.
7. git init hardening — the created repo must have no hooks, no remotes, no
   credential helper, and no template-inherited content: ``git init`` must
   be invoked with an explicit empty template (source-level assertion, plus
   behavioral proof via a poisoned ``init.templateDir``).

Positive control: an ordinary workspace under ``tmp_path`` is still accepted
(guard against over-blocking).
"""

from __future__ import annotations

import inspect
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import pipeline.workspace as ws
from pipeline.workspace import (
    create_workspace,
    normalize_workspace_path,
    validate_workspace,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


def _init_repo_with_commit(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    _git("commit", "--allow-empty", "-m", "init", cwd=path)
    return path


def _is_root() -> bool:
    return hasattr(os, "getuid") and os.getuid() == 0


def _assert_no_leak(text: str, extra_secrets: list[str] | None = None) -> None:
    """Assert *text* carries no internal filesystem structure."""
    secrets = [
        str(REPO_ROOT),
        f"/{REPO_ROOT.name}/",
        "Traceback",
        ".py",
        "site-packages",
    ] + (extra_secrets or [])
    for secret in secrets:
        assert secret not in text, f"leaked internal detail {secret!r} in: {text!r}"


@pytest.fixture
def clean_home(tmp_path, monkeypatch):
    """Point HOME at tmp_path so ~/.ssh deny tests are hermetic."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


# ---------------------------------------------------------------------------
# 1. WorkspaceSecurityError exists and is a ValueError
# ---------------------------------------------------------------------------


class TestWorkspaceSecurityError:
    def test_module_exports_workspace_security_error(self):
        assert hasattr(ws, "WorkspaceSecurityError"), (
            "pipeline.workspace must define WorkspaceSecurityError"
        )

    def test_workspace_security_error_is_a_value_error(self):
        assert issubclass(ws.WorkspaceSecurityError, ValueError)

    def test_normalize_raises_security_error_for_traversal(self):
        with pytest.raises(ws.WorkspaceSecurityError):
            normalize_workspace_path("/tmp/../etc")

    def test_validate_and_create_convert_security_error_to_ok_false(self, tmp_path):
        # The dict-returning API must never raise, including for security
        # rejections (pre-existing contract from test_workspace_validate.py).
        for fn in (validate_workspace, create_workspace):
            result = fn("/tmp/../etc")
            assert result["ok"] is False
            assert result["error"]


# ---------------------------------------------------------------------------
# 2. Path traversal — every encoding variant is denied
# ---------------------------------------------------------------------------


class TestTraversalVariantsDenied:
    @pytest.mark.parametrize(
        "raw",
        [
            "/tmp/../etc",
            "/tmp/foo/../foo",
            "/tmp/./../etc",
            "/tmp/%2e%2e/etc",  # percent-encoded dots
            "/tmp/%2E%2E/etc",  # uppercase percent-encoding
            "/tmp/%252e%252e/etc",  # double-encoded
            "/tmp/..%2fetc",  # encoded separator
            "/tmp/..\\etc",  # backslash separator
            "/tmp/dir\\..\\..\\etc",
            "/tmp/\x00../etc",  # NUL byte smuggling
            "/tmp/…/etc",  # unicode look-alike that naive decoders fold to ..
            "/tmp/dir/../../..",  # multi-segment climb
            "/../etc/passwd",
        ],
    )
    def test_traversal_variant_is_denied(self, raw):
        with pytest.raises(ValueError):
            normalize_workspace_path(raw)

    def test_traversal_denied_for_create_too(self):
        result = create_workspace("/tmp/../etc/pwned")
        assert result["ok"] is False
        assert result["error"]

    def test_traversal_error_does_not_leak_resolved_path(self):
        with pytest.raises(ValueError) as excinfo:
            normalize_workspace_path("/tmp/../etc/passwd")
        _assert_no_leak(str(excinfo.value), extra_secrets=["/etc/passwd"])

    def test_traversal_via_symlinked_parent_component_is_denied(self, tmp_path):
        # A literal '..' hidden behind a symlinked intermediate component:
        # link -> tmp_path/real, then link/../secret climbs OUT via the
        # symlink's parent. The raw '..' segment alone must already deny it.
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        with pytest.raises(ValueError):
            normalize_workspace_path(str(link / ".." / "secret"))


# ---------------------------------------------------------------------------
# 3. Symlink attacks — final and intermediate components
# ---------------------------------------------------------------------------


class TestSymlinkAttacksDenied:
    def test_final_component_symlink_is_denied(self, tmp_path):
        sensitive = tmp_path / "sensitive"
        sensitive.mkdir()
        link = tmp_path / "workspace"
        link.symlink_to(sensitive)
        with pytest.raises(ValueError):
            normalize_workspace_path(str(link))

    def test_intermediate_component_symlink_is_denied(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        link = tmp_path / "shim"
        link.symlink_to(outside)
        with pytest.raises(ValueError):
            normalize_workspace_path(str(link / "sub" / "workspace"))

    def test_symlink_to_nonexistent_target_is_denied(self, tmp_path):
        # A dangling symlink must not be treated as "path does not exist, OK
        # to create" — creating through it would materialize the target.
        link = tmp_path / "dangling"
        link.symlink_to(tmp_path / "nowhere")
        with pytest.raises(ValueError):
            normalize_workspace_path(str(link))

    def test_create_rejects_symlinked_target(self, tmp_path):
        sensitive = tmp_path / "sensitive"
        sensitive.mkdir()
        link = tmp_path / "workspace"
        link.symlink_to(sensitive)
        result = create_workspace(str(link))
        assert result["ok"] is False
        assert not (sensitive / ".git").exists(), "must not init through symlink"

    def test_create_rejects_intermediate_symlink(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        link = tmp_path / "shim"
        link.symlink_to(outside)
        result = create_workspace(str(link / "ws"))
        assert result["ok"] is False
        assert not (outside / "ws" / ".git").exists()

    def test_validate_rejects_symlinked_repo(self, tmp_path):
        repo = _init_repo_with_commit(tmp_path / "repo")
        link = tmp_path / "repo-link"
        link.symlink_to(repo)
        result = validate_workspace(str(link))
        assert result["ok"] is False

    def test_system_anchor_symlinks_are_tolerated(self, tmp_path):
        # macOS: /tmp -> /private/tmp. A path THROUGH a system anchor symlink
        # to a legitimate location must still work, or every tmp_path test
        # would over-block. Only component symlinks the caller controls are
        # rejected. No side effects: the target is never created.
        anchor = Path(tempfile.gettempdir())
        if not anchor.is_symlink():
            pytest.skip("system temp dir is not a symlink on this platform")
        target = anchor / "ws-anchor-probe-nonexistent"
        assert normalize_workspace_path(str(target)) == target.resolve()


# ---------------------------------------------------------------------------
# 4. Sensitive-location deny list — fails CLOSED
# ---------------------------------------------------------------------------


class TestSensitiveLocationDenyList:
    @pytest.mark.parametrize(
        "sensitive",
        ["/etc", "/System", "/usr", "/bin", "/sbin", "/var/root", "/boot"],
    )
    def test_system_roots_denied(self, sensitive):
        with pytest.raises(ValueError):
            normalize_workspace_path(sensitive)
        with pytest.raises(ValueError):
            normalize_workspace_path(sensitive + "/subdir")

    def test_etc_child_denied_for_create(self):
        result = create_workspace("/etc/pwned-ws")
        assert result["ok"] is False

    def test_system_child_denied_for_create(self):
        result = create_workspace("/System/pwned-ws")
        assert result["ok"] is False

    def test_user_ssh_dir_denied(self, clean_home):
        ssh = clean_home / ".ssh"
        ssh.mkdir()
        with pytest.raises(ValueError):
            normalize_workspace_path(str(ssh))
        result = create_workspace(str(ssh / "ws"))
        assert result["ok"] is False
        assert not (ssh / "ws" / ".git").exists()

    def test_ssh_lookalike_prefix_not_overblocked(self, clean_home):
        # .ssh-config is NOT .ssh; the deny entry must match the exact
        # component, not a string prefix.
        lookalike = clean_home / ".ssh-config" / "ws"
        result = create_workspace(str(lookalike))
        assert result["ok"] is True

    def test_pipeline_repo_root_denied(self, tmp_path, monkeypatch):
        # The pipeline's own checkout: creating a workspace here would nest a
        # git repo inside the pipeline's repo and pollute its status.
        fake_repo_root = tmp_path / "pipeline-checkout"
        fake_repo_root.mkdir()
        monkeypatch.setattr(ws, "REPO_ROOT", fake_repo_root)
        with pytest.raises(ValueError):
            normalize_workspace_path(str(fake_repo_root))
        result = create_workspace(str(fake_repo_root / "ws"))
        assert result["ok"] is False
        assert not (fake_repo_root / "ws" / ".git").exists()

    def test_repo_root_detection_fails_closed(self, tmp_path, monkeypatch):
        # If REPO_ROOT detection errors, the check must DENY, not fall open.
        # REPO_ROOT is a Path (or Path-like); a broken environment is
        # simulated by pointing it at a path whose inspection fails.
        class BadPath:
            def __fspath__(self):
                return str(tmp_path / "ws")

            def resolve(self, *a, **k):
                raise OSError("simulated fs failure")

        monkeypatch.setattr(ws, "REPO_ROOT", BadPath())
        with pytest.raises(ValueError):
            normalize_workspace_path(str(tmp_path / "ws"))

    def test_deny_list_wins_over_allow_list(self, tmp_path, monkeypatch, clean_home):
        # An operator (or attacker-controlled config) that allow-lists /etc
        # must NOT be able to punch through the deny list.
        monkeypatch.setattr(ws, "ALLOWED_CREATE_ROOTS", [Path("/etc")])
        result = create_workspace("/etc/pwned-ws")
        assert result["ok"] is False

    def test_malformed_deny_list_entry_fails_closed(self, tmp_path, monkeypatch):
        # A non-Path/non-string entry must not crash the check into
        # accepting the path.
        monkeypatch.setattr(ws, "DENY_LIST_ROOTS", [42, None, object()])
        with pytest.raises(ValueError):
            normalize_workspace_path(str(tmp_path / "ws"))

    def test_deny_check_error_fails_closed(self, tmp_path, monkeypatch):
        # If resolving a deny-list root raises, deny rather than allow.
        class BadPath:
            def resolve(self, *a, **k):
                raise OSError("simulated fs failure")

        monkeypatch.setattr(ws, "DENY_LIST_ROOTS", [BadPath()])
        with pytest.raises(ValueError):
            normalize_workspace_path(str(tmp_path / "ws"))

    def test_deny_list_roots_is_module_level_and_extensible(self):
        # Membership assertion only: later stories may add entries.
        assert isinstance(ws.DENY_LIST_ROOTS, list)
        names = {str(p) for p in ws.DENY_LIST_ROOTS}
        assert any("/etc" in n for n in names)
        assert any("/System" in n for n in names)
        assert any(".ssh" in n for n in names)

    @pytest.mark.skipif(sys.platform != "darwin", reason="/etc -> /private/etc symlink resolution is macOS-only; on Linux /private/etc is an ordinary nonexistent path")
    def test_deny_list_covers_resolved_etc_not_symlink_spelling(self):
        # On macOS /etc is a symlink to /private/etc; the deny list must
        # still catch a caller who spells the target directly.
        with pytest.raises(ValueError):
            normalize_workspace_path("/private/etc/pwned-ws")


# ---------------------------------------------------------------------------
# 5. Allow-list for create (validate stays usable for existing repos)
# ---------------------------------------------------------------------------


class TestCreateAllowList:
    def test_allowed_create_roots_exists_and_has_defaults(self):
        assert isinstance(ws.ALLOWED_CREATE_ROOTS, list)
        assert ws.ALLOWED_CREATE_ROOTS, "must have at least one default root"
        resolved = {
            Path(os.path.expanduser(str(p))).resolve() for p in ws.ALLOWED_CREATE_ROOTS
        }
        assert Path(tempfile.gettempdir()).resolve() in resolved
        assert Path.home().resolve() in resolved

    def test_create_outside_allow_list_is_denied(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ws, "ALLOWED_CREATE_ROOTS", [tmp_path / "allowed"])
        result = create_workspace(str(tmp_path / "elsewhere" / "ws"))
        assert result["ok"] is False
        assert not (tmp_path / "elsewhere" / "ws" / ".git").exists()

    def test_create_inside_allow_list_succeeds(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ws, "ALLOWED_CREATE_ROOTS", [tmp_path])
        result = create_workspace(str(tmp_path / "ws"))
        assert result["ok"] is True

    def test_malformed_allow_list_entry_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ws, "ALLOWED_CREATE_ROOTS", [None, 42])
        result = create_workspace(str(tmp_path / "ws"))
        assert result["ok"] is False

    def test_allow_list_error_fails_closed(self, tmp_path, monkeypatch):
        class BadPath:
            def resolve(self, *a, **k):
                raise OSError("simulated fs failure")

        monkeypatch.setattr(ws, "ALLOWED_CREATE_ROOTS", [BadPath()])
        result = create_workspace(str(tmp_path / "ws"))
        assert result["ok"] is False

    def test_validate_is_not_allow_listed(self, tmp_path):
        # validate_workspace must keep working for existing repos anywhere
        # legitimate (pre-existing tests init repos under tmp_path and expect
        # ok=True); only the deny list applies to it.
        repo = _init_repo_with_commit(tmp_path / "repo")
        assert validate_workspace(str(repo))["ok"] is True


# ---------------------------------------------------------------------------
# 6. Error hygiene — no internal paths, no stack traces
# ---------------------------------------------------------------------------


class TestErrorHygiene:
    def test_sanitize_error_message_exists(self):
        assert callable(getattr(ws, "sanitize_error_message", None)), (
            "pipeline.workspace must define sanitize_error_message"
        )

    def test_sanitize_strips_repo_root(self):
        leaked = f"boom at {REPO_ROOT}/pipeline/workspace.py:42"
        cleaned = ws.sanitize_error_message(leaked)
        assert str(REPO_ROOT) not in cleaned
        assert "boom" in cleaned, "must keep the useful part of the message"

    def test_sanitize_strips_traceback_and_py_paths(self):
        leaked = (
            "Traceback (most recent call last):\n"
            '  File "/usr/lib/python3/os.py", line 1, in module\n'
            "OSError: permission denied"
        )
        cleaned = ws.sanitize_error_message(leaked)
        assert "Traceback" not in cleaned
        assert ".py" not in cleaned

    def test_sanitize_strips_symlink_target(self, tmp_path):
        secret = tmp_path / "secret-place"
        secret.mkdir()
        cleaned = ws.sanitize_error_message(f"failed at {secret}")
        assert str(secret) not in cleaned

    def test_sanitize_never_raises(self):
        # Fail-closed hygiene: even garbage input must not blow up the
        # error path itself.
        for garbage in (None, 42, b"bytes", "", "plain message"):
            ws.sanitize_error_message(garbage)  # must not raise

    def test_create_error_messages_do_not_leak_internal_paths(self, tmp_path):
        result = create_workspace("/etc/pwned-ws")
        assert result["ok"] is False
        _assert_no_leak(str(result["error"]))

    def test_validate_error_messages_do_not_leak_internal_paths(self):
        result = validate_workspace("/etc/../etc/pwned")
        assert result["ok"] is False
        _assert_no_leak(str(result["error"]))

    def test_normalize_error_does_not_leak_repo_root(self):
        with pytest.raises(ValueError) as excinfo:
            normalize_workspace_path("/etc/pwned")
        _assert_no_leak(str(excinfo.value))

    def test_http_detail_has_no_internal_paths_or_traceback(self):
        # The route maps result["error"] straight into HTTPException detail;
        # asserting on the module-level error strings is the load-bearing
        # check (the route test below proves the pass-through).
        from fastapi.testclient import TestClient

        from app import dashboard as d

        client = TestClient(d.app)
        resp = client.post("/api/workspace", json={"path": "/etc/pwned", "create": True})
        assert resp.status_code in (400, 403)
        detail = resp.text
        _assert_no_leak(detail)
        assert "Traceback" not in detail

    def test_http_detail_for_traversal_is_generic(self):
        from fastapi.testclient import TestClient

        from app import dashboard as d

        client = TestClient(d.app)
        resp = client.post(
            "/api/workspace", json={"path": "/tmp/../etc/pwned", "create": True}
        )
        assert resp.status_code in (400, 403)
        assert ".." not in resp.text or "must not contain" in resp.text


# ---------------------------------------------------------------------------
# 7. git init hardening — no template inheritance, no hooks/remotes/creds
# ---------------------------------------------------------------------------


class TestGitInitHardening:
    def test_git_init_invoked_with_explicit_empty_template(self):
        # Source-level pin: the subprocess call must pass --template= (an
        # explicit empty template) so init.templateDir / /usr/share templates
        # can never inject hooks or config into the new repo.
        src = inspect.getsource(ws)
        assert re.search(r"--template", src), (
            "create_workspace must pass an explicit empty --template= to git init"
        )

    def test_created_repo_has_no_hooks(self, tmp_path):
        result = create_workspace(str(tmp_path / "ws"))
        assert result["ok"] is True
        hooks = list((tmp_path / "ws" / ".git" / "hooks").glob("*"))
        executable = [
            h for h in hooks if h.is_file() and h.stat().st_mode & stat.S_IXUSR
        ]
        assert executable == [], f"template hooks leaked: {executable}"

    def test_created_repo_has_no_remotes(self, tmp_path):
        create_workspace(str(tmp_path / "ws"))
        r = _git("remote", "-v", cwd=tmp_path / "ws")
        assert r.stdout.strip() == ""

    def test_created_repo_has_no_credential_helper(self, tmp_path):
        create_workspace(str(tmp_path / "ws"))
        r = _git("config", "--get", "credential.helper", cwd=tmp_path / "ws")
        assert r.returncode != 0 or r.stdout.strip() == ""

    def test_created_repo_local_config_has_no_credential_helper(self, tmp_path):
        # The local config must carry an EXPLICIT credential.helper entry
        # with an empty value (git treats an empty helper value as "reset
        # the helper list"), written directly into .git/config.
        repo_path = tmp_path / "ws"
        create_workspace(str(repo_path))
        config_text = (repo_path / ".git" / "config").read_text()
        assert "[credential]" in config_text
        assert re.search(r"^\s*helper\s*=\s*$", config_text, re.MULTILINE)  # (a) helper present, EMPTY value
        proc = subprocess.run(
            ["git", "config", "--get", "credential.helper"],
            cwd=repo_path, capture_output=True, text=True, check=False,
        )
        assert proc.stdout.strip() == ""                                # (b) behavioral reset proof
        assert proc.returncode == 0  # exit 0 = key PRESENT with empty value (helpers disabled); exit 1 would mean the key is absent and inherited helpers still apply
        assert not (repo_path / ".git" / "security-reset").exists()     # (c) indirection file gone
        assert "include.path" not in config_text                        # (d) no include directive
        assert "security-reset" not in config_text

    def test_create_workspace_does_not_raise_when_git_config_fails(
        self, tmp_path, monkeypatch
    ):
        # Regression (review WS-SEC-01): the credential-helper reset must
        # stay non-fatal.  A non-zero exit from the `git config --local
        # credential.helper ""` call (read-only .git/config, disk-full ->
        # exit 128) raises CalledProcessError under check=True, which is a
        # SubprocessError, NOT an OSError — so the surrounding except
        # OSError cannot catch it and it would escape create_workspace,
        # breaking the module's "never raises" contract.
        real_run = subprocess.run

        def failing_run(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "config"] and "credential.helper" in cmd:
                # Emulate exactly what real subprocess.run(check=True) does
                # on a non-zero exit (read-only .git/config / disk-full ->
                # exit 128).
                if kwargs.get("check"):
                    raise subprocess.CalledProcessError(128, cmd)
                return subprocess.CompletedProcess(cmd, 128, "", "fatal: could not lock config file")
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", failing_run)

        repo = tmp_path / "repo"
        ws.create_workspace(str(repo))            # must NOT raise
        assert repo.exists()

        # Follow-up call: the swallowed failure must not poison anything.
        ws.create_workspace(str(tmp_path / "repo2"))  # must NOT raise either
        assert (tmp_path / "repo2").exists()

    def test_created_repo_config_has_no_include_path(self, tmp_path):
        repo_path = tmp_path / "ws"
        create_workspace(str(repo_path))
        config_text = (repo_path / ".git" / "config").read_text()
        assert "include.path" not in config_text

    def test_poisoned_template_dir_cannot_inject_hook(self, tmp_path, monkeypatch):
        # Behavioral proof: even with init.templateDir set in the environment
        # the created repo must stay hook-free.
        poison = tmp_path / "poison-template"
        (poison / "hooks").mkdir(parents=True)
        hook = poison / "hooks" / "post-checkout"
        hook.write_text("#!/bin/sh\necho pwned\n")
        hook.chmod(0o755)
        monkeypatch.setenv("GIT_TEMPLATE_DIR", str(poison))
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global-gitconfig"))
        (tmp_path / "global-gitconfig").write_text(
            f"[init]\n\ttemplateDir = {poison}\n"
            "[credential]\n\thelper = !evil-command\n"
        )
        result = create_workspace(str(tmp_path / "ws"))
        assert result["ok"] is True
        hooks = list((tmp_path / "ws" / ".git" / "hooks").glob("*"))
        executable = [
            h for h in hooks if h.is_file() and h.stat().st_mode & stat.S_IXUSR
        ]
        assert executable == [], "poisoned template injected an executable hook"
        proc = subprocess.run(
            ["git", "config", "--get", "credential.helper"],
            cwd=tmp_path / "ws", capture_output=True, text=True, check=False,
        )
        assert proc.stdout.strip() == ""   # poisoned GIT_CONFIG_GLOBAL must not leak through
        local = subprocess.run(
            ["git", "config", "--list", "--local"],
            cwd=tmp_path / "ws", capture_output=True, text=True, check=True,
        ).stdout
        assert "evil" not in local

    def test_created_repo_has_no_inherited_config_from_global_includeif(
        self, tmp_path, monkeypatch
    ):
        # A global gitconfig that conditionally includes a template with
        # settings must not leak into the new repo's local config.
        global_cfg = tmp_path / "global-gitconfig"
        global_cfg.write_text("[init]\n\tdefaultBranch = main\n")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_cfg))
        result = create_workspace(str(tmp_path / "ws"))
        assert result["ok"] is True
        local = (tmp_path / "ws" / ".git" / "config").read_text()
        assert "evil" not in local.lower()


# ---------------------------------------------------------------------------
# 8. Positive controls — ordinary workspaces keep working
# ---------------------------------------------------------------------------


class TestPositiveControls:
    def test_ordinary_create_under_tmp_path_is_accepted(self, tmp_path):
        target = tmp_path / "my-workspace"
        result = create_workspace(str(target))
        assert result["ok"] is True, f"over-blocking regression: {result}"
        assert result["error"] is None
        assert (target / ".git").is_dir()

    def test_ordinary_nested_create_under_tmp_path_is_accepted(self, tmp_path):
        target = tmp_path / "deep" / "nested" / "ws"
        result = create_workspace(str(target))
        assert result["ok"] is True
        assert target.is_dir()

    def test_ordinary_validate_under_tmp_path_is_accepted(self, tmp_path):
        repo = _init_repo_with_commit(tmp_path / "repo")
        result = validate_workspace(str(repo))
        assert result["ok"] is True
        assert result["path"] == str(repo.resolve())

    def test_ordinary_home_relative_create_is_accepted(self, clean_home):
        result = create_workspace(str(clean_home / "projects" / "ws"))
        assert result["ok"] is True

    def test_tilde_path_still_expands(self, clean_home):
        result = normalize_workspace_path("~/projects/ws")
        assert result == (clean_home / "projects" / "ws").resolve()

    def test_empty_and_whitespace_and_none_still_rejected(self):
        for bad in (None, "", "   "):
            with pytest.raises(ValueError):
                normalize_workspace_path(bad)

    def test_relative_path_still_rejected(self):
        with pytest.raises(ValueError):
            normalize_workspace_path("relative/path")

    def test_non_string_path_rejected(self):
        for bad in (123, 4.5, ["/tmp/x"], {"p": "/tmp/x"}):
            with pytest.raises(ValueError):
                normalize_workspace_path(bad)  # type: ignore[arg-type]

    def test_http_happy_path_still_works(self, tmp_path, plan_dir):
        # plan_dir is required here: without it, create=True's POST
        # /api/workspace genuinely writes active_workspace.json into the
        # real, unmocked PLAN_DIR (found live 2026-09-05 - this test had
        # left a stale reference to a since-deleted pytest tmp_path
        # sitting in the real ~/.claude/plans/active_workspace.json,
        # polluting any later, unrelated test/production read of the
        # active workspace).
        from fastapi.testclient import TestClient

        from app import dashboard as d

        client = TestClient(d.app)
        resp = client.post(
            "/api/workspace", json={"path": str(tmp_path / "ws"), "create": True}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# 9. Defense in depth — the service layer enforces the same rules
# ---------------------------------------------------------------------------


class TestServiceLayerEnforcement:
    """The module is the primary control, but the service must not become a
    bypass: PipelineService.resolve_workspace (the route's delegate) must
    surface the same denials."""

    def test_service_denies_traversal_on_create(self, plan_dir):
        from pipeline import server as p

        result = p._service.resolve_workspace("/tmp/../etc/pwned", create=True)
        assert result["ok"] is False
        _assert_no_leak(str(result["error"]))

    def test_service_denies_sensitive_location_on_create(self, plan_dir):
        from pipeline import server as p

        result = p._service.resolve_workspace("/etc/pwned-ws", create=True)
        assert result["ok"] is False
        _assert_no_leak(str(result["error"]))

    def test_service_denial_is_not_recorded_as_recent_workspace(self, plan_dir):
        from pipeline import server as p
        from pipeline import store as store_mod

        store = store_mod.FileStore()
        before = store.get_recent_workspaces()
        p._service.resolve_workspace("/etc/pwned-ws", create=True)
        assert store.get_recent_workspaces() == before

    def test_service_accepts_ordinary_workspace(self, plan_dir, tmp_path):
        from pipeline import server as p

        result = p._service.resolve_workspace(str(tmp_path / "ws"), create=True)
        assert result["ok"] is True, result


class TestSymlinkTargetToleranceRemoved:
    """WS-SEC-02: only the link's own EXACT spelling may be an anchor.

    A caller-controlled symlink whose *target* happens to resolve into a
    system anchor (e.g. the system temp dir itself) must still be denied:
    the target-based half of the old tolerate condition contradicted the
    documented "only EXACT anchor spellings are tolerated" policy.
    """

    def test_symlink_to_system_tempdir_is_denied(self, tmp_path):
        # Target = the system temp dir itself, which IS in the anchor set
        # (on macOS via /private/tmp; on Linux /tmp's realpath is /tmp).
        # The spelled link path is not an anchor, so this must be denied.
        link = tmp_path / "foo"
        link.symlink_to(tempfile.gettempdir())
        with pytest.raises(ValueError):
            normalize_workspace_path(str(link))

    def test_intermediate_symlink_to_tempdir_is_denied(self, tmp_path):
        # Same attack, one level deeper: the symlink is an intermediate
        # component of the requested path.
        link = tmp_path / "link"
        link.symlink_to(tempfile.gettempdir())
        with pytest.raises(ValueError):
            normalize_workspace_path(str(link / "ws"))