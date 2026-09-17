"""Systemd user-unit template for the advance-scheduler daemon.

The repo already ships a launchd agent for the advance-scheduler daemon
(``launchd/com.fagan.pipeline.advance-scheduler.plist.template``, installed to
``~/Library/LaunchAgents``).  This story adds the Linux/systemd counterpart: a
USER unit template at ``systemd/com.fagan.pipeline.advance-scheduler.service.template``
that a later story's generator script installs into ``~/.config/systemd/user/``
and drives with ``systemctl --user``.

Design decisions pinned here (each one is a graded assertion, so a later edit
that silently reverses one fails loudly):

* ``{{REPO_ROOT}}`` / ``{{HOME}}`` stay LITERAL placeholder tokens in the
  template -- substitution is the generator script's job, so no real
  machine-specific path (this machine's home directory, this checkout's path)
  may appear in the shipped file.
* ``Type=simple`` + ``Restart=always`` + ``RestartSec=5`` is the systemd
  equivalent of the plist's ``KeepAlive=true`` (restart on death, 5s backoff so
  a crash loop does not spin hot).
* ``WantedBy=default.target`` is the systemd equivalent of the plist's
  ``RunAtLoad=true`` (starts when the unit is enabled).
* NO ``EnvironmentFile=`` and NO embedded ``PIPELINE_*`` tuning keys:
  ``pipeline/__init__.py`` already loads ``<repo_root>/.pipeline.env`` (or
  ``PIPELINE_ENV_FILE``) into the process environment at import time for every
  entrypoint regardless of how it was launched, so systemd does not need its
  own copy of that mechanism.  Only ``PATH`` is set, matching the plist's own
  PATH-only non-MLX-specific entries.
* It is a USER unit: it must never require root and must never reference
  ``/etc/systemd/system``.

RED state (the intended TDD state, not a bug in this suite): the template file
does not exist yet, so every test below fails on the missing artifact.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE = (
    _REPO_ROOT / "systemd" / "com.fagan.pipeline.advance-scheduler.service.template"
)

# The exact bytes the story specifies, including the single trailing newline.
_EXPECTED_CONTENT = """\
[Unit]
Description=Fagan pipeline advance-scheduler daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={{REPO_ROOT}}
ExecStart={{REPO_ROOT}}/.venv/bin/python3 -m pipeline.scheduler_daemon
Restart=always
RestartSec=5
StandardOutput=append:{{REPO_ROOT}}/advance-scheduler.log
StandardError=append:{{REPO_ROOT}}/advance-scheduler.err.log
Environment=PATH={{HOME}}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin

[Install]
WantedBy=default.target
"""

_SECTION_HEADERS = ("[Unit]", "[Service]", "[Install]")

# Any PIPELINE_* key (e.g. PIPELINE_AUTONOMY=full) would be an embedded tuning
# var; the .pipeline.env delegation design forbids them here.
_PIPELINE_KEY_RE = re.compile(r"\bPIPELINE_[A-Z0-9_]+")


_MISSING_MSG = (
    f"missing systemd user-unit template: {_TEMPLATE} "
    "(expected at systemd/com.fagan.pipeline.advance-scheduler.service.template)"
)


@pytest.fixture(scope="module")
def template_text() -> str:
    """The template's text, or ``""`` when the artifact is absent.

    Deliberately non-asserting: an assertion inside a fixture surfaces as a
    pytest ERROR, and this suite must report clean FAILURES while the
    implementation is still missing.  Tests that need real content call
    ``_require_template`` first.
    """
    if not _TEMPLATE.is_file():
        return ""
    return _TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def template_bytes() -> bytes:
    if not _TEMPLATE.is_file():
        return b""
    return _TEMPLATE.read_bytes()


def _require_template(text: str) -> None:
    """Guard for tests whose assertions would otherwise pass on an empty file."""
    assert text.strip(), _MISSING_MSG


# --------------------------------------------------------------------------
# (1) the file exists at that path
# --------------------------------------------------------------------------


def test_template_exists_at_expected_path() -> None:
    assert _TEMPLATE.is_file(), (
        f"expected the systemd user-unit template at {_TEMPLATE}"
    )


def test_template_is_a_sibling_of_launchd_not_inside_it() -> None:
    """New top-level ``systemd/`` directory, sibling to ``launchd/``."""
    assert _TEMPLATE.parent == _REPO_ROOT / "systemd"
    assert _TEMPLATE.parent.name == "systemd"
    assert _TEMPLATE.parent.parent == _REPO_ROOT
    assert "launchd" not in _TEMPLATE.parts


def test_launchd_artifacts_are_untouched() -> None:
    """The story must not touch anything under launchd/."""
    launchd = _REPO_ROOT / "launchd"
    assert launchd.is_dir(), "launchd/ directory disappeared"
    assert (
        launchd / "com.fagan.pipeline.advance-scheduler.plist.template"
    ).is_file(), "launchd advance-scheduler template was removed or moved"


def test_template_is_not_empty(template_text: str) -> None:
    assert template_text.strip(), "template is empty"


# --------------------------------------------------------------------------
# (2) no real machine-specific path; placeholders stay literal
# --------------------------------------------------------------------------


def test_placeholders_are_present_literally(template_text: str) -> None:
    assert "{{REPO_ROOT}}" in template_text, "{{REPO_ROOT}} placeholder missing"
    assert "{{HOME}}" in template_text, "{{HOME}} placeholder missing"


def test_no_absolute_path_under_this_machines_home(template_text: str) -> None:
    _require_template(template_text)
    home = str(Path.home())
    assert home not in template_text, (
        f"template leaks this machine's home directory ({home}); "
        "{{HOME}} must stay a literal placeholder"
    )


def test_no_absolute_path_to_this_checkout(template_text: str) -> None:
    _require_template(template_text)
    repo_root = str(_REPO_ROOT)
    assert repo_root not in template_text, (
        f"template leaks this checkout's path ({repo_root}); "
        "{{REPO_ROOT}} must stay a literal placeholder"
    )


def test_no_other_absolute_home_style_paths(template_text: str) -> None:
    """No ``/Users/<name>`` or ``/home/<name>`` style absolute paths at all."""
    _require_template(template_text)
    offenders = re.findall(r"/(?:Users|home)/[A-Za-z0-9._-]+", template_text)
    assert offenders == [], f"machine-specific absolute paths present: {offenders}"


def test_placeholders_are_not_substituted(template_text: str) -> None:
    """Every path-bearing directive still carries its placeholder token."""
    assert "WorkingDirectory={{REPO_ROOT}}" in template_text
    assert "ExecStart={{REPO_ROOT}}/.venv/bin/python3" in template_text
    assert "{{HOME}}/.local/bin" in template_text


# --------------------------------------------------------------------------
# (3) valid systemd unit syntax: each section header on its own line
# --------------------------------------------------------------------------


@pytest.mark.parametrize("header", _SECTION_HEADERS)
def test_section_header_is_its_own_line(template_text: str, header: str) -> None:
    lines = template_text.splitlines()
    assert header in lines, (
        f"{header} must appear as its own section header line; got lines={lines!r}"
    )


def test_section_headers_appear_in_order(template_text: str) -> None:
    _require_template(template_text)
    lines = template_text.splitlines()
    positions = [lines.index(header) for header in _SECTION_HEADERS]
    assert positions == sorted(positions), (
        f"section headers out of order: {list(zip(_SECTION_HEADERS, positions))}"
    )


def test_every_nonblank_line_is_a_header_or_key_value(template_text: str) -> None:
    """No stray prose/XML/plist leftovers: each line is ``Key=Value`` or a header."""
    _require_template(template_text)
    for lineno, line in enumerate(template_text.splitlines(), start=1):
        if not line.strip():
            continue
        if line in _SECTION_HEADERS:
            continue
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9]*=.*", line), (
            f"line {lineno} is not valid systemd unit syntax: {line!r}"
        )


def test_no_plist_or_xml_leftovers(template_text: str) -> None:
    _require_template(template_text)
    for marker in ("<?xml", "<plist", "<dict>", "<key>", "<string>"):
        assert marker not in template_text, f"plist/XML leftover in unit: {marker}"


# --------------------------------------------------------------------------
# (4) restart + enable semantics
# --------------------------------------------------------------------------


def test_restart_always_present(template_text: str) -> None:
    assert "Restart=always" in template_text, (
        "Restart=always is the systemd equivalent of the plist's KeepAlive=true"
    )


def test_restart_sec_backoff_present(template_text: str) -> None:
    assert "RestartSec=5" in template_text, (
        "RestartSec=5 is the 5s backoff that keeps a crash loop from spinning hot"
    )


def test_type_simple_present(template_text: str) -> None:
    assert "Type=simple" in template_text


def test_wanted_by_default_target_present(template_text: str) -> None:
    assert "WantedBy=default.target" in template_text, (
        "WantedBy=default.target is the systemd equivalent of RunAtLoad=true"
    )


def test_wanted_by_is_not_multi_user_target(template_text: str) -> None:
    """A user unit must not be enabled into the system-wide multi-user target."""
    _require_template(template_text)
    assert "WantedBy=multi-user.target" not in template_text


def test_exec_start_runs_the_scheduler_daemon_module(template_text: str) -> None:
    assert (
        "ExecStart={{REPO_ROOT}}/.venv/bin/python3 -m pipeline.scheduler_daemon"
        in template_text
    )


def test_working_directory_is_repo_root(template_text: str) -> None:
    assert "WorkingDirectory={{REPO_ROOT}}" in template_text


def test_stdout_and_stderr_are_appended_to_repo_logs(template_text: str) -> None:
    assert (
        "StandardOutput=append:{{REPO_ROOT}}/advance-scheduler.log" in template_text
    )
    assert (
        "StandardError=append:{{REPO_ROOT}}/advance-scheduler.err.log"
        in template_text
    )


def test_description_names_the_daemon(template_text: str) -> None:
    assert "Description=Fagan pipeline advance-scheduler daemon" in template_text


def test_network_online_ordering_present(template_text: str) -> None:
    assert "After=network-online.target" in template_text
    assert "Wants=network-online.target" in template_text


def test_path_environment_matches_plist_non_mlx_entries(template_text: str) -> None:
    assert (
        "Environment=PATH={{HOME}}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        in template_text
    )


# --------------------------------------------------------------------------
# (5) negative: no EnvironmentFile=, no embedded PIPELINE_* tuning vars
# --------------------------------------------------------------------------


def test_no_environment_file_directive(template_text: str) -> None:
    _require_template(template_text)
    assert "EnvironmentFile=" not in template_text, (
        "pipeline/__init__.py already loads .pipeline.env at import time; "
        "systemd must not carry its own EnvironmentFile= copy"
    )


def test_no_pipeline_tuning_keys(template_text: str) -> None:
    _require_template(template_text)
    found = _PIPELINE_KEY_RE.findall(template_text)
    assert found == [], (
        f"embedded PIPELINE_* tuning keys are forbidden in this unit: {found}"
    )


def test_only_path_is_set_in_the_environment(template_text: str) -> None:
    """Only PATH is set, matching the plist's PATH-only non-MLX entries."""
    env_lines = [
        line
        for line in template_text.splitlines()
        if line.startswith("Environment=")
    ]
    assert env_lines == [
        "Environment=PATH={{HOME}}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    ], f"unexpected Environment= lines: {env_lines}"


# --------------------------------------------------------------------------
# user-unit scope: never root, never /etc/systemd/system
# --------------------------------------------------------------------------


def test_never_references_system_wide_unit_directory(template_text: str) -> None:
    _require_template(template_text)
    assert "/etc/systemd/system" not in template_text, (
        "this is a systemd USER unit (~/.config/systemd/user/, systemctl --user); "
        "it must never reference /etc/systemd/system"
    )


def test_no_root_privilege_directives(template_text: str) -> None:
    _require_template(template_text)
    for directive in ("User=root", "Group=root", "PermissionsStartOnly="):
        assert directive not in template_text, (
            f"user unit must not require root: found {directive!r}"
        )


def test_no_sudo_or_systemctl_system_invocation(template_text: str) -> None:
    _require_template(template_text)
    assert "sudo" not in template_text
    assert "systemctl --system" not in template_text


# --------------------------------------------------------------------------
# verbatim content: byte for byte, including the trailing newline
# --------------------------------------------------------------------------


def test_content_is_verbatim(template_text: str) -> None:
    assert template_text == _EXPECTED_CONTENT, (
        "template content differs from the specified verbatim content"
    )


def test_has_exactly_one_trailing_newline(template_bytes: bytes) -> None:
    assert template_bytes.endswith(b"\n"), "template must end with a trailing newline"
    assert not template_bytes.endswith(b"\n\n"), (
        "template must end with exactly one trailing newline"
    )


def test_uses_lf_line_endings(template_bytes: bytes) -> None:
    _require_template(template_bytes)
    assert b"\r" not in template_bytes, "template must use LF line endings, not CRLF"


def test_no_trailing_whitespace_on_any_line(template_text: str) -> None:
    _require_template(template_text)
    offenders = [
        lineno
        for lineno, line in enumerate(template_text.splitlines(), start=1)
        if line != line.rstrip()
    ]
    assert offenders == [], f"trailing whitespace on lines: {offenders}"
