"""The installer requires the *resolved forge's* CLI (FORGE-S3).

``scripts/install_checks.py`` used to hardcode ``gh``. A repo whose active
forge is GitLab needs ``glab`` instead, so the doctor resolves the forge
(``PIPELINE_FORGE``, or the origin remote's host under ``auto``) and requires
exactly that forge's CLI - never both.

The base suite pins the doctor's shape (two top-level functions, stdlib-only
imports, six check names under the default GitHub forge, ``main`` returning 0
for a merely-missing tool), so the forge-aware requirement is expressed
through the existing ``collect_checks`` report plus a loud, non-zero exit from
``main`` when an explicitly configured forge's CLI is absent.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "install_checks.py"

_spec = importlib.util.spec_from_file_location("install_checks_forge", str(_SCRIPT))
ic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ic)

# Every tool the doctor can look for, including the GitLab CLI it must never
# require for a GitHub repo (and vice versa).
_ALL_TOOLS = ("python", "git", "gh", "glab", "claude", "ollama", "docker")


def _which_stub(present):
    """A ``shutil.which`` stand-in: a fake path for names in ``present``."""

    def _which(name, *args, **kwargs):
        return f"/fake/toolchain/bin/{name}" if name in present else None

    return _which


def _collect(present):
    return ic.collect_checks(
        which=_which_stub(set(present)), py_version=(3, 11)
    )


def test_installer_requires_gh_when_forge_is_github(monkeypatch):
    monkeypatch.setenv("PIPELINE_FORGE", "github")

    # gh missing, glab present: only the resolved forge's CLI may be required.
    by_name = {c["name"]: c for c in _collect(set(_ALL_TOOLS) - {"gh"})}

    assert "glab" not in by_name, "a GitHub repo must not require the GitLab CLI"
    gh = by_name["gh"]
    assert gh["required"] is True
    assert gh["status"] == "missing"
    assert "gh" in gh["hint"].lower()
    assert "github" in gh["hint"].lower(), "the hint must name the resolved forge"

    # ...and with gh present the requirement is satisfied.
    assert {c["name"]: c for c in _collect(_ALL_TOOLS)}["gh"]["status"] == "ok"


def test_installer_requires_glab_when_forge_is_gitlab(monkeypatch):
    monkeypatch.setenv("PIPELINE_FORGE", "gitlab")

    # glab missing, gh present: gh must not stand in for the GitLab CLI.
    by_name = {c["name"]: c for c in _collect(set(_ALL_TOOLS) - {"glab"})}

    assert "gh" not in by_name, "a GitLab repo must not require the GitHub CLI"
    glab = by_name["glab"]
    assert glab["required"] is True
    assert glab["status"] == "missing"
    assert "glab" in glab["hint"].lower()
    assert "gitlab" in glab["hint"].lower(), "the hint must name the resolved forge"

    # ...and with glab present the requirement is satisfied.
    assert {c["name"]: c for c in _collect(_ALL_TOOLS)}["glab"]["status"] == "ok"


def _patch_which(monkeypatch, present):
    """Route ``main``'s collect_checks through a stubbed ``which``.

    ``collect_checks`` binds ``which=shutil.which`` as a default at def time,
    so patching ``shutil.which`` cannot reach it; the stub is injected the
    supported way instead, leaving the forge resolution under test intact.
    """
    real_collect = ic.collect_checks
    monkeypatch.setattr(
        ic,
        "collect_checks",
        lambda *args, **kwargs: real_collect(
            which=_which_stub(present), py_version=(3, 11)
        ),
    )


def test_installer_fails_loudly_when_required_cli_missing(monkeypatch, capsys):
    monkeypatch.setenv("PIPELINE_FORGE", "gitlab")
    _patch_which(monkeypatch, set(_ALL_TOOLS) - {"glab"})

    exit_code = ic.main([])

    assert exit_code != 0, "a missing required CLI must fail the run"
    err = capsys.readouterr().err
    assert "glab" in err, "the message must name the missing CLI"
    assert "gitlab" in err, "the message must name the resolved forge"


def test_installer_does_not_fail_when_required_cli_present(monkeypatch, capsys):
    monkeypatch.setenv("PIPELINE_FORGE", "gitlab")
    _patch_which(monkeypatch, _ALL_TOOLS)

    assert ic.main([]) == 0
    assert "glab" not in capsys.readouterr().err


@pytest.mark.parametrize("forge", ["github", "gitlab"])
def test_installer_rejects_an_unknown_forge_value(monkeypatch, forge):
    monkeypatch.setenv("PIPELINE_FORGE", "bitbucket")

    with pytest.raises(ValueError, match="PIPELINE_FORGE"):
        _collect(_ALL_TOOLS)


def test_main_reports_an_unknown_forge_without_a_traceback(monkeypatch, capsys):
    """A bad PIPELINE_FORGE is a clean non-zero error, not a crash.

    ``collect_checks`` raises ``ValueError`` for an unknown forge; ``main``
    must surface it as a one-line error on stderr and exit non-zero rather
    than letting a traceback escape the doctor script.
    """
    monkeypatch.setenv("PIPELINE_FORGE", "bitbucket")

    assert ic.main([]) == 2
    err = capsys.readouterr().err
    assert "PIPELINE_FORGE" in err
    assert "bitbucket" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# auto resolution: the origin remote's host decides the forge
# ---------------------------------------------------------------------------
# The doctor must parse every remote form git accepts - https://, ssh:// and
# scp-like ``git@host:path`` - and take the HOST, not the scheme. A bare
# ``split(":")[0]`` yields "https" for an https remote, which would silently
# pick the wrong forge.


def _patch_origin(monkeypatch, url):
    """Make the origin-remote seam answer ``url``."""
    monkeypatch.setattr(ic, "_REMOTE_URL", lambda repo_root: url)


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://gitlab.com/group/project.git", "glab"),
        ("https://github.com/owner/project.git", "gh"),
        ("ssh://git@gitlab.com/group/project.git", "glab"),
        ("git@gitlab.com:group/project.git", "glab"),
        ("git@github.com:owner/project.git", "gh"),
    ],
)
def test_auto_resolves_the_forge_from_the_origin_host(monkeypatch, url, expected):
    monkeypatch.delenv("PIPELINE_FORGE", raising=False)
    _patch_origin(monkeypatch, url)

    by_name = {c["name"]: c for c in _collect(_ALL_TOOLS)}

    assert expected in by_name, f"{url} must resolve to the {expected} CLI"
    assert by_name[expected]["required"] is True
    other = "gh" if expected == "glab" else "glab"
    assert other not in by_name, "only the resolved forge's CLI is required"


def test_auto_with_an_unrecognised_host_stays_report_only(monkeypatch, capsys):
    """The repo may not be the pipeline's: an unknown host must not fail."""
    monkeypatch.delenv("PIPELINE_FORGE", raising=False)
    _patch_origin(monkeypatch, "https://git.example.com/group/project.git")
    _patch_which(monkeypatch, set(_ALL_TOOLS) - {"gh", "glab"})

    assert ic.main([]) == 0
    assert "error:" not in capsys.readouterr().err


def test_auto_with_a_confident_host_fails_loudly_when_its_cli_is_missing(
    monkeypatch, capsys
):
    """A host that confidently names a forge makes its CLI mandatory."""
    monkeypatch.delenv("PIPELINE_FORGE", raising=False)
    _patch_origin(monkeypatch, "https://gitlab.com/group/project.git")
    _patch_which(monkeypatch, set(_ALL_TOOLS) - {"glab"})

    exit_code = ic.main([])

    assert exit_code != 0, "a confidently resolved forge's missing CLI must fail"
    err = capsys.readouterr().err
    assert "glab" in err, "the message must name the missing CLI"
    assert "gitlab" in err, "the message must name the resolved forge"


# ---------------------------------------------------------------------------
# the module docstring and argparse description are the contract the next
# story (scripts/install.sh) reads: they must not promise "always returns 0"
# or a hardcoded gh now that main() can exit 1 and 2 and the required CLI is
# the resolved forge's.
# ---------------------------------------------------------------------------


def test_module_docstring_documents_the_nonzero_exits():
    """The docstring must name the new exit codes, not "always returns 0"."""
    doc = " ".join((ic.__doc__ or "").split())
    assert "always returns 0" not in doc, "the docstring still promises exit 0"
    assert "returns 0" in doc, "the docstring must state the success exit code"
    assert "1 when the resolved forge's CLI is missing" in doc
    assert "2 when ``PIPELINE_FORGE`` names an unknown forge" in doc


def test_module_docstring_does_not_claim_presence_only_collect_checks():
    """collect_checks resolves the forge, so "stays presence-only" is false."""
    doc = " ".join((ic.__doc__ or "").split())
    assert "stays presence-only" not in doc
    assert "does no auth probing" in doc


def test_module_docstring_names_the_forge_cli_rule():
    """The docstring must say the required CLI is the resolved forge's."""
    doc = " ".join((ic.__doc__ or "").split())
    assert "gh for github, glab for gitlab" in doc
    assert "resolved forge's CLI" in doc


def test_argparse_description_is_not_advisory_only(capsys):
    """``--help`` must not still call the doctor "advisory only"."""
    with pytest.raises(SystemExit):
        ic.main(["--help"])
    out = capsys.readouterr().out
    assert "advisory only" not in out
    assert "resolved" in out and "forge" in out
