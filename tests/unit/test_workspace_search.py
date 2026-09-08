"""Tests for ``PipelineService.search_workspace`` (pipeline/service.py)
and its route ``GET /api/workspace/search`` (app/dashboard.py).

Story: add exactly one new method to ``PipelineService``, placed near the
existing workspace accessors (``read_workspace_file`` /
``list_workspace_directory``):

    search_workspace(self, pattern: str, *, max_results: int = 200) -> dict

Behavior contract (every branch returns a dict, never raises):

- no active workspace -> ``{'ok': False, 'error': 'no active workspace'}``
- empty string or non-string pattern
                      -> ``{'ok': False, 'error': 'invalid pattern'}``
- otherwise the implementation MUST run grep via ``subprocess.run`` with the
  command passed as a LIST::

      ['grep', '-rn', '-I', '--exclude-dir=.git', '-e', pattern, '--',
       <workspace_root>]

  with ``capture_output=True, text=True, timeout=10``.  CRITICAL: never
  ``shell=True`` and never string-interpolate *pattern* into a shell command
  -- the pattern travels as a single argv element to grep, so it is
  structurally impossible to use for shell injection regardless of content.
- grep exit code 0    -> matches found: split stdout into lines (no trailing
  empty element), truncate to *max_results*, return
  ``{'ok': True, 'matches': <lines>, 'truncated': <bool>}`` where
  ``truncated`` is True iff more lines existed than *max_results*.
- grep exit code 1    -> no matches (grep's normal "nothing matched" signal,
  NOT an error): ``{'ok': True, 'matches': [], 'truncated': False}``.
- grep exit code > 1, or ``FileNotFoundError`` (grep not installed)
                      -> ``{'ok': False, 'error': 'search failed'}`` -- the
  response must NOT include grep's stderr (Secure by Design: no internal
  filesystem structure in error responses).
- ``subprocess.TimeoutExpired``
                      -> ``{'ok': False, 'error': 'search timed out'}``.

Route: ``GET /api/workspace/search`` with a REQUIRED ``pattern`` query
parameter (str).  The route calls ``_service.search_workspace(pattern)`` and
follows the sibling routes' exact 400-on-not-ok pattern: not-ok result ->
``HTTPException(400, detail=<service error string>)``; ok result -> the dict
returned verbatim.  Covered by the global ``require_api_key`` dependency.

The active workspace is stubbed to a ``tmp_path`` directory both at the
instance level (``service.get_active_workspace``) and at the module seam
(``pipeline.service._store.get_active_workspace``), mirroring the sibling
workspace-capability test files.  The grep subprocess is faked (by patching
``subprocess.run`` -- and the module's own ``run`` binding if it holds a
``from subprocess import run`` copy) for the exit-code/argv contract tests,
and run FOR REAL against ``tmp_path`` for the behavioral tests (injection
safety, match format, truncation, .git exclusion, binary skipping).

Every test here fails with ``AttributeError`` (method does not exist yet) or
a 404/assertion (route does not exist yet) until the implementation lands --
that is the correct RED state.
"""

from __future__ import annotations

import glob
import inspect
import json
import os
import shutil
import subprocess

import pytest

import pipeline.service as service_mod
from pipeline.service import PipelineService

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def service():
    """A bare PipelineService (the project's established construction)."""
    return PipelineService()


def _stub_active_workspace(monkeypatch, service, path):
    """Force ``get_active_workspace`` to report *path* (str) or None.

    Patches both seams the implementation could plausibly read: the instance
    method (the ``self.get_active_workspace()`` dispatch that
    ``read_workspace_file`` uses) and the module-level ``_store`` free
    variable, so the tests pass either way.
    """
    monkeypatch.setattr(service, "get_active_workspace", lambda: path)
    store = getattr(service_mod, "_store", None)
    if store is not None and hasattr(store, "get_active_workspace"):
        monkeypatch.setattr(
            store, "get_active_workspace", lambda *a, **k: path
        )


def _install_fake_grep(
    monkeypatch,
    *,
    returncode=0,
    stdout="",
    stderr="",
    exc=None,
):
    """Replace the grep subprocess with a recording fake.

    Patches ``subprocess.run`` (covers ``import subprocess`` +
    ``subprocess.run(...)``) and, if ``pipeline.service`` holds its own
    ``run`` binding that is literally ``subprocess.run`` (a
    ``from subprocess import run`` import), that binding too.  Returns the
    recorded call so tests can assert the exact argv / kwargs contract.
    """
    recorded = {}

    def fake_run(args, **kwargs):
        recorded["args"] = args
        recorded["kwargs"] = kwargs
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(
            args=args, returncode=returncode, stdout=stdout, stderr=stderr
        )

    real_run = subprocess.run
    module_binding = getattr(service_mod, "run", None)
    monkeypatch.setattr(subprocess, "run", fake_run)
    if module_binding is not None and module_binding is real_run:
        monkeypatch.setattr(service_mod, "run", fake_run)
    return recorded


def _path_variants(path):
    """Both spellings of *path* (macOS /var vs /private/var symlinks)."""
    return [str(path), os.path.realpath(str(path))]


requires_grep = pytest.mark.skipif(
    shutil.which("grep") is None, reason="grep binary not available"
)


# ---------------------------------------------------------------------------
# Service level: PipelineService.search_workspace
# ---------------------------------------------------------------------------


def test_search_workspace_method_exists_with_contracted_signature():
    """The method exists with signature (pattern, *, max_results=200)."""
    assert hasattr(PipelineService, "search_workspace"), (
        "PipelineService.search_workspace is missing"
    )
    sig = inspect.signature(PipelineService.search_workspace)
    names = list(sig.parameters)
    assert names == ["self", "pattern", "max_results"]
    pattern = sig.parameters["pattern"]
    assert pattern.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert pattern.default is inspect.Parameter.empty
    max_results = sig.parameters["max_results"]
    assert max_results.kind is inspect.Parameter.KEYWORD_ONLY
    assert max_results.default == 200


def test_no_active_workspace(service, monkeypatch):
    """No active workspace -> the fixed 'no active workspace' error dict."""
    _stub_active_workspace(monkeypatch, service, None)
    recorded = _install_fake_grep(monkeypatch)
    result = service.search_workspace("anything")
    assert result == {"ok": False, "error": "no active workspace"}
    # The workspace check happens before any subprocess is spawned.
    assert "args" not in recorded


def test_empty_pattern_is_invalid(service, monkeypatch):
    """Empty pattern string -> 'invalid pattern', and grep is never run."""
    _stub_active_workspace(monkeypatch, service, "/tmp/whatever")
    recorded = _install_fake_grep(monkeypatch)
    result = service.search_workspace("")
    assert result == {"ok": False, "error": "invalid pattern"}
    assert "args" not in recorded


@pytest.mark.parametrize(
    "bad_pattern",
    [None, 123, 4.5, b"needle", ["needle"], {"needle": 1}, True],
)
def test_non_string_patterns_are_invalid(service, monkeypatch, bad_pattern):
    """Any non-str pattern -> 'invalid pattern' (never a TypeError/crash)."""
    _stub_active_workspace(monkeypatch, service, "/tmp/whatever")
    recorded = _install_fake_grep(monkeypatch)
    result = service.search_workspace(bad_pattern)
    assert result == {"ok": False, "error": "invalid pattern"}
    assert "args" not in recorded


def test_whitespace_pattern_is_valid_and_reaches_grep(service, monkeypatch):
    """A single space is a non-empty str -> valid; it must reach grep."""
    _stub_active_workspace(monkeypatch, service, "/tmp/whatever")
    recorded = _install_fake_grep(monkeypatch, returncode=1)
    result = service.search_workspace(" ")
    assert result == {"ok": True, "matches": [], "truncated": False}
    assert recorded["args"][5] == " "


def test_grep_argv_is_a_list_with_exact_elements_and_no_shell(
    service, monkeypatch, tmp_path
):
    """The grep command is a LIST argv, exact elements, never shell=True.

    This is the structural anti-shell-injection contract: the pattern is one
    argv element handed to grep, never interpolated into a shell string.
    """
    root = str(tmp_path)
    _stub_active_workspace(monkeypatch, service, root)
    nasty = '; touch /tmp/pwned_$$ #'
    recorded = _install_fake_grep(monkeypatch, returncode=1)
    service.search_workspace(nasty)

    args = recorded["args"]
    assert isinstance(args, list), "grep command must be a list, not a string"
    assert all(isinstance(element, str) for element in args)
    assert len(args) == 8
    assert args[0:5] == ["grep", "-rn", "-I", "--exclude-dir=.git", "-e"]
    # The whole pattern, metacharacters and all, is ONE argv element.
    assert args[5] == nasty
    assert args[6] == "--"
    assert os.path.realpath(args[7]) == os.path.realpath(root)

    kwargs = recorded["kwargs"]
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert kwargs.get("timeout") == 10
    assert not kwargs.get("shell", False), "shell=True is forbidden"


def test_exit_zero_splits_lines_without_trailing_empty(
    service, monkeypatch
):
    """Exit 0 -> stdout split into lines; no trailing empty element."""
    _stub_active_workspace(monkeypatch, service, "/tmp/whatever")
    _install_fake_grep(
        monkeypatch, returncode=0, stdout="alpha\nbeta\ngamma\n"
    )
    result = service.search_workspace("needle")
    assert result == {
        "ok": True,
        "matches": ["alpha", "beta", "gamma"],
        "truncated": False,
    }
    assert set(result) == {"ok", "matches", "truncated"}


def test_exit_zero_with_empty_stdout(service, monkeypatch):
    """Exit 0 with empty stdout -> matches == [], truncated False."""
    _stub_active_workspace(monkeypatch, service, "/tmp/whatever")
    _install_fake_grep(monkeypatch, returncode=0, stdout="")
    result = service.search_workspace("needle")
    assert result == {"ok": True, "matches": [], "truncated": False}


def test_truncation_to_max_results(service, monkeypatch):
    """More lines than max_results -> exactly max_results, truncated True."""
    _stub_active_workspace(monkeypatch, service, "/tmp/whatever")
    _install_fake_grep(
        monkeypatch,
        returncode=0,
        stdout="l1\nl2\nl3\nl4\nl5\n",
    )
    result = service.search_workspace("needle", max_results=3)
    assert result == {
        "ok": True,
        "matches": ["l1", "l2", "l3"],
        "truncated": True,
    }


def test_exactly_max_results_is_not_truncated(service, monkeypatch):
    """Boundary: line count == max_results -> all kept, truncated False."""
    _stub_active_workspace(monkeypatch, service, "/tmp/whatever")
    _install_fake_grep(
        monkeypatch,
        returncode=0,
        stdout="l1\nl2\nl3\nl4\nl5\n",
    )
    result = service.search_workspace("needle", max_results=5)
    assert result == {
        "ok": True,
        "matches": ["l1", "l2", "l3", "l4", "l5"],
        "truncated": False,
    }


def test_exit_one_means_no_matches_not_an_error(service, monkeypatch):
    """grep exit 1 is the normal 'nothing matched' signal, NOT a failure."""
    _stub_active_workspace(monkeypatch, service, "/tmp/whatever")
    _install_fake_grep(monkeypatch, returncode=1, stdout="", stderr="")
    result = service.search_workspace("needle")
    assert result == {"ok": True, "matches": [], "truncated": False}
    assert result["ok"] is True


def test_exit_greater_than_one_is_search_failed_without_stderr(
    service, monkeypatch
):
    """Exit > 1 -> 'search failed'; grep's stderr must NOT leak back."""
    _stub_active_workspace(monkeypatch, service, "/tmp/whatever")
    _install_fake_grep(
        monkeypatch,
        returncode=2,
        stdout="",
        stderr="grep: /etc/shadow: Permission denied",
    )
    result = service.search_workspace("needle")
    assert result == {"ok": False, "error": "search failed"}
    blob = json.dumps(result)
    assert "Permission denied" not in blob
    assert "/etc/shadow" not in blob


def test_missing_grep_binary_is_search_failed(service, monkeypatch):
    """FileNotFoundError (grep not installed) -> 'search failed'."""
    _stub_active_workspace(monkeypatch, service, "/tmp/whatever")
    _install_fake_grep(
        monkeypatch,
        exc=FileNotFoundError(2, "No such file or directory", "grep"),
    )
    result = service.search_workspace("needle")
    assert result == {"ok": False, "error": "search failed"}


def test_timeout_is_search_timed_out(service, monkeypatch):
    """subprocess.TimeoutExpired -> 'search timed out'."""
    _stub_active_workspace(monkeypatch, service, "/tmp/whatever")
    recorded = _install_fake_grep(
        monkeypatch,
        exc=subprocess.TimeoutExpired(cmd=["grep"], timeout=10),
    )
    result = service.search_workspace("needle")
    assert result == {"ok": False, "error": "search timed out"}
    assert recorded["kwargs"].get("timeout") == 10


# ---------------------------------------------------------------------------
# Service level: real grep against a tmp_path workspace
# ---------------------------------------------------------------------------


@requires_grep
def test_shell_metacharacter_pattern_causes_no_side_effect(
    service, monkeypatch, tmp_path
):
    """A shell-metacharacter pattern is just grep input -- no side effects.

    If the implementation ever interpolated *pattern* into a shell command,
    ``; touch ... #`` would execute ``touch`` and leave a file behind.  The
    structural guarantee (list argv, no shell) means the search completes as
    an ordinary (here zero-match) grep and NOTHING is created.
    """
    seed = tmp_path / "notes.txt"
    seed.write_text("hello world\nplain text line\n", encoding="utf-8")
    before_workspace = sorted(p.name for p in tmp_path.iterdir())
    before_tmp_pwned = sorted(glob.glob("/tmp/pwned_*"))

    _stub_active_workspace(monkeypatch, service, str(tmp_path))
    result = service.search_workspace('; touch /tmp/pwned_$$ #')

    assert result["ok"] is True
    assert result["matches"] == []
    assert result["truncated"] is False

    # No side effect inside the workspace...
    assert sorted(p.name for p in tmp_path.iterdir()) == before_workspace
    # ...none in /tmp from the brief's example payload...
    assert sorted(glob.glob("/tmp/pwned_*")) == before_tmp_pwned
    # ...and a second metacharacter flavor (backticks + expansion) is inert.
    result2 = service.search_workspace(f"`touch {tmp_path}/pwned_marker`")
    assert result2["ok"] is True
    assert sorted(p.name for p in tmp_path.iterdir()) == before_workspace
    assert not (tmp_path / "pwned_marker").exists()


@requires_grep
def test_known_string_match_includes_path_and_line_number(
    service, monkeypatch, tmp_path
):
    """A known string is found with grep's standard ``-n`` output format."""
    seed = tmp_path / "notes.txt"
    seed.write_text(
        "first line\nsecond line\nNEEDLE_TOKEN_XYZ here\nfourth\n",
        encoding="utf-8",
    )
    _stub_active_workspace(monkeypatch, service, str(tmp_path))
    result = service.search_workspace("NEEDLE_TOKEN_XYZ")

    assert result["ok"] is True
    assert result["truncated"] is False
    matches = result["matches"]
    assert len(matches) == 1
    match = matches[0]
    # grep -rn format: <path>:<line>:<text>  -> path and line number present.
    assert ":3:" in match
    assert any(variant in match for variant in _path_variants(seed))
    assert "NEEDLE_TOKEN_XYZ" in match


@requires_grep
def test_no_match_is_ok_true_with_empty_matches(
    service, monkeypatch, tmp_path
):
    """A pattern matching nothing -> ok True, matches [] -- NOT an error."""
    seed = tmp_path / "notes.txt"
    seed.write_text("ordinary text\nmore ordinary text\n", encoding="utf-8")
    _stub_active_workspace(monkeypatch, service, str(tmp_path))
    result = service.search_workspace("ZZZ_ABSENT_TOKEN_ZZZ")
    assert result == {"ok": True, "matches": [], "truncated": False}


@requires_grep
def test_real_grep_truncates_to_max_results(service, monkeypatch, tmp_path):
    """Real grep output is truncated to exactly max_results entries."""
    seed = tmp_path / "haystack.txt"
    seed.write_text(
        "\n".join(f"needle line {i}" for i in range(1, 11)) + "\n",
        encoding="utf-8",
    )
    _stub_active_workspace(monkeypatch, service, str(tmp_path))

    truncated = service.search_workspace("needle", max_results=4)
    assert truncated["ok"] is True
    assert len(truncated["matches"]) == 4
    assert truncated["truncated"] is True
    assert all("needle line" in m for m in truncated["matches"])

    exact = service.search_workspace("needle", max_results=10)
    assert exact["ok"] is True
    assert len(exact["matches"]) == 10
    assert exact["truncated"] is False


@requires_grep
def test_git_directory_is_excluded(service, monkeypatch, tmp_path):
    """``--exclude-dir=.git``: matches inside .git never surface."""
    seed = tmp_path / "code.txt"
    seed.write_text("SECRET_MARKER here\n", encoding="utf-8")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(
        "SECRET_MARKER in git metadata\n", encoding="utf-8"
    )
    _stub_active_workspace(monkeypatch, service, str(tmp_path))
    result = service.search_workspace("SECRET_MARKER")
    assert result["ok"] is True
    for match in result["matches"]:
        assert ".git" not in match
    assert any("code.txt" in m for m in result["matches"])


@requires_grep
def test_binary_files_are_skipped(service, monkeypatch, tmp_path):
    """``-I``: binary files are ignored, only text matches come back."""
    seed = tmp_path / "text.txt"
    seed.write_text("BINARY_SKIP_MARKER here\n", encoding="utf-8")
    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\x00\x01\x02BINARY_SKIP_MARKER\x00\x03")
    _stub_active_workspace(monkeypatch, service, str(tmp_path))
    result = service.search_workspace("BINARY_SKIP_MARKER")
    assert result["ok"] is True
    for match in result["matches"]:
        assert "Binary" not in match
        assert "blob.bin" not in match
    assert any("text.txt" in m for m in result["matches"])


# ---------------------------------------------------------------------------
# Route level: GET /api/workspace/search (app/dashboard.py)
# ---------------------------------------------------------------------------
#
# Follows the EXACT pattern of the sibling workspace routes: on a not-ok
# result raise HTTPException(status_code=400, detail=result['error']);
# otherwise return the result dict verbatim.  The conftest autouse fixture
# injects the correct X-Pipeline-Api-Key header into every TestClient, so
# these requests carry valid auth; the service is mocked so only the route
# wiring is under test.


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app import dashboard

    return TestClient(dashboard.app)


def test_route_exists_exactly_once_as_get(client):
    """Exactly one route serves GET /api/workspace/search."""
    from app import dashboard

    matching = [
        r
        for r in dashboard.app.routes
        if getattr(r, "path", "") == "/api/workspace/search"
    ]
    assert len(matching) == 1
    assert "GET" in getattr(matching[0], "methods", set())


def test_route_maps_pattern_query_to_service(client, monkeypatch):
    """GET /api/workspace/search?pattern=... delegates to the service."""
    from app import dashboard

    captured = {}

    def fake_search(pattern, **kwargs):
        captured["pattern"] = pattern
        return {"ok": True, "matches": ["m1"], "truncated": False}

    monkeypatch.setattr(dashboard._service, "search_workspace", fake_search)
    response = client.get(
        "/api/workspace/search", params={"pattern": "NEEDLE"}
    )
    assert response.status_code == 200
    assert captured["pattern"] == "NEEDLE"
    assert response.json() == {
        "ok": True,
        "matches": ["m1"],
        "truncated": False,
    }


def test_route_pattern_query_is_required(client, monkeypatch):
    """The 'pattern' query parameter is required: missing -> 422."""
    from app import dashboard

    monkeypatch.setattr(
        dashboard._service,
        "search_workspace",
        lambda *a, **k: {"ok": True, "matches": [], "truncated": False},
    )
    response = client.get("/api/workspace/search")
    assert response.status_code == 422


def test_route_returns_400_with_service_error(client, monkeypatch):
    """ok False -> HTTP 400 whose detail is the service's error string."""
    from app import dashboard

    monkeypatch.setattr(
        dashboard._service,
        "search_workspace",
        lambda *a, **k: {"ok": False, "error": "invalid pattern"},
    )
    response = client.get(
        "/api/workspace/search", params={"pattern": "whatever"}
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid pattern"


def test_route_400_detail_is_verbatim_service_error(client, monkeypatch):
    """Every not-ok service error surfaces verbatim as the 400 detail."""
    from app import dashboard

    for error in (
        "no active workspace",
        "invalid pattern",
        "search failed",
        "search timed out",
    ):

        def fake_search(*a, _error=error, **k):
            return {"ok": False, "error": _error}

        monkeypatch.setattr(
            dashboard._service, "search_workspace", fake_search
        )
        response = client.get(
            "/api/workspace/search", params={"pattern": "x"}
        )
        assert response.status_code == 400
        assert response.json()["detail"] == error


def test_route_rejects_missing_auth_header(monkeypatch):
    """The global require_api_key dependency covers the new route too."""
    from fastapi.testclient import TestClient

    from app import dashboard

    monkeypatch.setattr(
        dashboard._service,
        "search_workspace",
        lambda *a, **k: {"ok": True, "matches": [], "truncated": False},
    )
    with TestClient(
        dashboard.app, headers={"X-Pipeline-Api-Key": "wrong-key"}
    ) as bad:
        response = bad.get("/api/workspace/search", params={"pattern": "x"})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Regression: reviewer-blocking failure modes that violated the documented
# "every branch returns a dict, never raises" contract.
#
# Bug 1 -- NUL byte in pattern: Starlette percent-decodes "%00", so the route
# (and any direct caller) can hand search_workspace a str containing U+0000.
# That string passes the existing empty/non-str check, but Python refuses NUL
# bytes in argv, so subprocess.run(['grep', '-e', 'a\x00b', ...]) raises
# ValueError("embedded null byte"), which the except clause did not catch ->
# HTTP 500.  Contract: a NUL byte is just another invalid pattern.
#
# Bug 2 -- non-UTF-8 grep output: under LC_ALL=C (typical Docker/CI) grep
# treats every byte as valid, so -I does not filter a Latin-1 text file and
# emits the raw high byte in the matched line; text=True then decodes stdout
# STRICTLY as UTF-8 and raises UnicodeDecodeError -> HTTP 500.  The sibling
# read_workspace_file already treats undecodable bytes as a handled failure
# mode, so the search path must decode with errors='replace' instead.
#
# These tests run the REAL subprocess (no fake grep) so the pre-fix failure
# is exactly the reviewer-verified exception, not a stub artifact.
# ---------------------------------------------------------------------------


def test_nul_byte_pattern_is_invalid(service, monkeypatch, tmp_path):
    """A pattern containing U+0000 -> 'invalid pattern', never ValueError.

    Regression: "a\\x00b" passed the empty/non-str check and then blew up
    inside subprocess.run with ``ValueError: embedded null byte`` (Python
    refuses NUL bytes in argv), escaping as an HTTP 500.  It must be rejected
    with the same dict the empty/non-str rejection returns.
    """
    _stub_active_workspace(monkeypatch, service, str(tmp_path))
    result = service.search_workspace("a\x00b")
    assert result == {"ok": False, "error": "invalid pattern"}


def test_route_nul_byte_pattern_is_400_not_500(client, monkeypatch, tmp_path):
    """GET /api/workspace/search?pattern=a%00b -> 400, never a 500.

    Regression: Starlette percent-decodes %00, so the handler received the
    3-char string "a\\x00b", which passed validation and made the real
    subprocess.run raise ValueError -> unhandled -> HTTP 500.  The route must
    map the service's 'invalid pattern' rejection to 400 like every other
    not-ok result.  The REAL service method runs (only the active workspace
    is pointed at tmp_path) so the pre-fix failure is the exact ValueError.
    """
    from app import dashboard

    monkeypatch.setattr(
        dashboard._service, "get_active_workspace", lambda: str(tmp_path)
    )
    store = getattr(service_mod, "_store", None)
    if store is not None and hasattr(store, "get_active_workspace"):
        monkeypatch.setattr(
            store, "get_active_workspace", lambda *a, **k: str(tmp_path)
        )
    # Literal URL (not params=) so the raw %00 reaches the handler and is
    # percent-decoded into U+0000, exactly as a real client sends it.
    response = client.get("/api/workspace/search?pattern=a%00b")
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid pattern"


@requires_grep
def test_non_utf8_match_line_decoded_with_replacement(
    service, monkeypatch, tmp_path
):
    """A matched line with a non-UTF-8 byte -> ok with U+FFFD, never raise.

    Regression: under LC_ALL=C grep treats every byte as valid, so -I does
    not skip this Latin-1 text file and emits the raw 0xE9 byte in the
    matched line; text=True decoded stdout strictly as UTF-8 and raised
    UnicodeDecodeError ("'utf-8' codec can't decode byte 0xe9"), escaping as
    an HTTP 500.  Grep's output must be decoded with errors='replace' so the
    undecodable byte surfaces as U+FFFD instead of raising.
    """
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("LANG", "C")
    monkeypatch.delenv("LC_CTYPE", raising=False)
    latin1_file = tmp_path / "menu.latin1.txt"
    # Binary write: no encoding layer may rewrite the raw 0xE9 byte.
    with open(latin1_file, "wb") as fh:
        fh.write(b"caf\xe9 menu\n")
    _stub_active_workspace(monkeypatch, service, str(tmp_path))
    result = service.search_workspace("caf")
    assert result["ok"] is True
    assert result["truncated"] is False
    assert any("caf\ufffd menu" in m for m in result["matches"]), result
