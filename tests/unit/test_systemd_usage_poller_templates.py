"""Systemd unit templates for the Fagan pipeline usage-poller.

The macOS deployment uses ``launchd/com.fagan.pipeline.usage-poller.plist``
(``StartInterval=60``).  The Linux/systemd equivalent is a *matched pair* of
templates under ``systemd/``:

  * ``com.fagan.pipeline.usage-poller.service.template`` - a ``Type=oneshot``
    unit.  systemd runs the command once and exits; that is the correct type
    for a short poll, not a long-running daemon.  A oneshot unit is therefore
    never ``Restart=``-ed and is never enabled/started directly.
  * ``com.fagan.pipeline.usage-poller.timer.template`` - the paired timer.
    ``OnUnitActiveSec=60`` is what re-triggers the service every 60 seconds
    (mirroring the plist's ``StartInterval=60``), and its
    ``[Install] WantedBy=timers.target`` is what an operator enables with
    ``systemctl --user enable --now com.fagan.pipeline.usage-poller.timer``.

Both files are *templates*: ``{{REPO_ROOT}}`` and ``{{HOME}}`` are left as
literal placeholder tokens for a later generator script to substitute, so no
real machine-specific path may be baked in.

These tests grade the templates only.  They are written to fail (at the
``FileNotFoundError`` raised by ``_read``) until the two template files exist.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
_SYSTEMD = _REPO / "systemd"

_SERVICE_NAME = "com.fagan.pipeline.usage-poller.service"
_TIMER_NAME = "com.fagan.pipeline.usage-poller.timer"

_SERVICE_TEMPLATE = _SYSTEMD / f"{_SERVICE_NAME}.template"
_TIMER_TEMPLATE = _SYSTEMD / f"{_TIMER_NAME}.template"

# The exact, verbatim template bodies required by the story.  Trailing
# newlines are normalised away by _normalized() so a missing/extra final
# newline is not graded, but every line and every character of every line is.
_SERVICE_EXPECTED = """\
[Unit]
Description=Fagan pipeline usage-poller (one-shot; triggered by the paired .timer unit, never run directly)

[Service]
Type=oneshot
WorkingDirectory={{REPO_ROOT}}
ExecStart={{REPO_ROOT}}/.venv/bin/python3 -c "import app.pipeline_mcp_server as p; p.check_usage()"
StandardOutput=append:{{REPO_ROOT}}/usage-poller.log
StandardError=append:{{REPO_ROOT}}/usage-poller.err.log
Environment=PATH={{HOME}}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin
"""

_TIMER_EXPECTED = """\
[Unit]
Description=Run the Fagan pipeline usage-poller every 60 seconds

[Timer]
OnBootSec=60
OnUnitActiveSec=60
Unit=com.fagan.pipeline.usage-poller.service

[Install]
WantedBy=timers.target
"""

# Real, machine-specific absolute paths that must never appear in a template.
_HARDCODED_PATH_RE = re.compile(
    r"(/Users/|/home/|/root/|/var/folders/|[A-Za-z]:\\Users\\)"
)

_PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _read(path: Path) -> str:
    """Read a template, failing loudly (and by name) when it is missing."""
    if not path.is_file():
        raise FileNotFoundError(f"required systemd template missing: {path}")
    return path.read_text(encoding="utf-8")


def _normalized(text: str) -> str:
    """Normalise line endings and strip trailing blank lines."""
    return text.replace("\r\n", "\n").rstrip("\n")


def _directive(text: str, key: str):
    """Return the value of the last ``key=`` directive, or None if absent.

    systemd uses last-one-wins for repeated directives, so the last match is
    the effective value.  Returns None (never raises) when the key is absent.
    """
    matches = re.findall(rf"^{re.escape(key)}=(.*)$", text, re.MULTILINE)
    if not matches:
        return None
    return matches[-1].strip()


def _hardcoded_paths(text: str) -> list:
    """Return every machine-specific absolute path fragment found in *text*."""
    return _HARDCODED_PATH_RE.findall(text)


def _service_text() -> str:
    return _read(_SERVICE_TEMPLATE)


def _timer_text() -> str:
    return _read(_TIMER_TEMPLATE)


# --------------------------------------------------------------------------
# (1) both files exist
# --------------------------------------------------------------------------
def test_systemd_directory_exists():
    assert _SYSTEMD.is_dir(), f"systemd/ directory missing at {_SYSTEMD}"


def test_service_template_exists():
    assert _SERVICE_TEMPLATE.is_file(), f"missing {_SERVICE_TEMPLATE}"


def test_timer_template_exists():
    assert _TIMER_TEMPLATE.is_file(), f"missing {_TIMER_TEMPLATE}"


def test_both_templates_are_non_empty():
    assert _service_text().strip(), "service template is empty"
    assert _timer_text().strip(), "timer template is empty"


def test_templates_are_a_matched_pair_with_matching_stems():
    # com.fagan.pipeline.usage-poller.{service,timer}.template
    assert _SERVICE_TEMPLATE.name == "com.fagan.pipeline.usage-poller.service.template"
    assert _TIMER_TEMPLATE.name == "com.fagan.pipeline.usage-poller.timer.template"
    # Same unit base name; only the .service/.timer suffix differs.
    assert _SERVICE_TEMPLATE.name.removesuffix(".service.template") == (
        _TIMER_TEMPLATE.name.removesuffix(".timer.template")
    )
    # Both halves of the pair must actually be on disk.
    assert _SERVICE_TEMPLATE.is_file(), f"missing {_SERVICE_TEMPLATE}"
    assert _TIMER_TEMPLATE.is_file(), f"missing {_TIMER_TEMPLATE}"


# --------------------------------------------------------------------------
# verbatim content
# --------------------------------------------------------------------------
def test_service_template_matches_required_content_verbatim():
    assert _normalized(_service_text()) == _normalized(_SERVICE_EXPECTED)


def test_timer_template_matches_required_content_verbatim():
    assert _normalized(_timer_text()) == _normalized(_TIMER_EXPECTED)


def test_service_template_has_unit_and_service_sections():
    text = _service_text()
    assert "[Unit]" in text
    assert "[Service]" in text


def test_timer_template_has_unit_timer_and_install_sections():
    text = _timer_text()
    assert "[Unit]" in text
    assert "[Timer]" in text
    assert "[Install]" in text


# --------------------------------------------------------------------------
# (2) Type=oneshot, and no Restart=
# --------------------------------------------------------------------------
def test_service_type_is_exactly_oneshot():
    assert _directive(_service_text(), "Type") == "oneshot"


def test_service_contains_type_oneshot_literal():
    assert "Type=oneshot" in _service_text()


def test_service_has_no_restart_directive():
    text = _service_text()
    assert "Restart=" not in text, "a Type=oneshot unit must not declare Restart="
    assert _directive(text, "Restart") is None


def test_service_has_no_restart_sec_directive():
    assert _directive(_service_text(), "RestartSec") is None


def test_service_is_not_a_long_running_daemon_type():
    # Type=simple/forking/notify would mean "keep running", which is wrong here.
    assert _directive(_service_text(), "Type") not in {
        "simple",
        "forking",
        "notify",
        "dbus",
    }


def test_service_has_exactly_one_execstart():
    lines = [ln for ln in _service_text().splitlines() if ln.startswith("ExecStart=")]
    assert len(lines) == 1, f"expected exactly one ExecStart= line, got {lines!r}"


def test_service_execstart_runs_the_usage_poller_check():
    value = _directive(_service_text(), "ExecStart")
    assert value is not None
    assert value.startswith("{{REPO_ROOT}}/.venv/bin/python3")
    assert "-c" in value
    assert "import app.pipeline_mcp_server as p; p.check_usage()" in value


def test_service_working_directory_is_repo_root_placeholder():
    assert _directive(_service_text(), "WorkingDirectory") == "{{REPO_ROOT}}"


def test_service_stdout_and_stderr_append_under_repo_root():
    text = _service_text()
    assert _directive(text, "StandardOutput") == "append:{{REPO_ROOT}}/usage-poller.log"
    assert (
        _directive(text, "StandardError") == "append:{{REPO_ROOT}}/usage-poller.err.log"
    )


def test_service_path_environment_uses_home_placeholder():
    assert (
        _directive(_service_text(), "Environment")
        == "PATH={{HOME}}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    )


def test_service_description_marks_it_one_shot_and_timer_triggered():
    description = _directive(_service_text(), "Description")
    assert description is not None
    assert "one-shot" in description
    assert "timer" in description


# --------------------------------------------------------------------------
# (3) the timer's Unit= names exactly the service from FILE 1
# --------------------------------------------------------------------------
def test_timer_unit_directive_names_exact_service_filename():
    assert _directive(_timer_text(), "Unit") == _SERVICE_NAME


def test_timer_unit_directive_is_not_a_mismatched_name():
    value = _directive(_timer_text(), "Unit")
    assert value is not None
    assert value.endswith(".service")
    assert value != _TIMER_NAME
    assert value != "com.fagan.pipeline.usage-poller"
    assert value == "com.fagan.pipeline.usage-poller.service"


def test_timer_unit_targets_the_paired_service_template_on_disk():
    value = _directive(_timer_text(), "Unit")
    assert value is not None
    assert (_SYSTEMD / f"{value}.template").is_file(), (
        f"timer Unit={value} does not correspond to a template in {_SYSTEMD}"
    )


def test_timer_has_exactly_one_unit_directive():
    lines = [ln for ln in _timer_text().splitlines() if ln.startswith("Unit=")]
    assert len(lines) == 1, f"expected exactly one Unit= line, got {lines!r}"


def test_timer_retriggers_every_60_seconds():
    text = _timer_text()
    assert _directive(text, "OnUnitActiveSec") == "60"
    assert _directive(text, "OnBootSec") == "60"


def test_timer_description_mentions_60_seconds():
    description = _directive(_timer_text(), "Description")
    assert description is not None
    assert "60 seconds" in description


def test_timer_has_no_execstart_of_its_own():
    # Only the .service runs the command; the .timer merely schedules it.
    assert _directive(_timer_text(), "ExecStart") is None
    assert "ExecStart=" not in _timer_text()


def test_timer_has_no_service_type_directive():
    assert _directive(_timer_text(), "Type") is None


def test_service_has_no_timer_directives():
    text = _service_text()
    assert _directive(text, "OnUnitActiveSec") is None
    assert _directive(text, "OnBootSec") is None
    assert "[Timer]" not in text


# --------------------------------------------------------------------------
# (4) only the timer is installed/enabled
# --------------------------------------------------------------------------
def test_timer_is_wanted_by_timers_target():
    assert "WantedBy=timers.target" in _timer_text()
    assert _directive(_timer_text(), "WantedBy") == "timers.target"


def test_timer_has_an_install_section():
    assert "[Install]" in _timer_text()


def test_service_has_no_wantedby_directive():
    text = _service_text()
    assert "WantedBy=" not in text, (
        "only the .timer is enabled, never the one-shot .service"
    )
    assert _directive(text, "WantedBy") is None


def test_service_has_no_install_section():
    assert "[Install]" not in _service_text()


def test_timer_has_no_restart_directive():
    assert _directive(_timer_text(), "Restart") is None


# --------------------------------------------------------------------------
# (5) no real machine-specific paths - placeholders only
# --------------------------------------------------------------------------
def test_service_template_contains_no_hardcoded_machine_paths():
    found = _hardcoded_paths(_service_text())
    assert found == [], (
        f"hardcoded machine-specific path(s) in service template: {found}"
    )


def test_timer_template_contains_no_hardcoded_machine_paths():
    found = _hardcoded_paths(_timer_text())
    assert found == [], f"hardcoded machine-specific path(s) in timer template: {found}"


def test_service_template_keeps_repo_root_and_home_as_literal_placeholders():
    text = _service_text()
    assert "{{REPO_ROOT}}" in text
    assert "{{HOME}}" in text
    # ...and they must be literal tokens, not already substituted values.
    assert _directive(text, "WorkingDirectory") == "{{REPO_ROOT}}"


def test_service_template_does_not_contain_the_real_repo_root_or_home():
    text = _service_text()
    home = os.path.expanduser("~")
    if home and home not in {"/", ""}:
        assert home not in text, (
            f"real home directory {home!r} leaked into service template"
        )
    repo = str(_REPO)
    if repo not in {"/", ""}:
        assert repo not in text, f"real repo root {repo!r} leaked into service template"


def test_timer_template_contains_no_placeholder_tokens_at_all():
    text = _timer_text()
    assert "{{REPO_ROOT}}" not in text
    assert "{{HOME}}" not in text
    assert _PLACEHOLDER_RE.findall(text) == []


def test_timer_template_does_not_contain_the_real_repo_root_or_home():
    text = _timer_text()
    home = os.path.expanduser("~")
    if home and home not in {"/", ""}:
        assert home not in text, (
            f"real home directory {home!r} leaked into timer template"
        )
    repo = str(_REPO)
    if repo not in {"/", ""}:
        assert repo not in text, f"real repo root {repo!r} leaked into timer template"


# --------------------------------------------------------------------------
# negative / boundary cases for the helpers themselves (guards against a
# vacuous check: the detectors must actually be able to fail)
# --------------------------------------------------------------------------
def test_hardcoded_path_detector_flags_a_macos_home_path():
    assert _hardcoded_paths("WorkingDirectory=/Users/someone/repo") == ["/Users/"]


def test_hardcoded_path_detector_flags_a_linux_home_path():
    assert _hardcoded_paths("ExecStart=/home/someone/repo/.venv/bin/python3") == [
        "/home/"
    ]


def test_hardcoded_path_detector_flags_a_root_path():
    assert _hardcoded_paths("WorkingDirectory=/root/repo") == ["/root/"]


def test_hardcoded_path_detector_is_clean_on_placeholder_text():
    assert _hardcoded_paths("WorkingDirectory={{REPO_ROOT}}") == []
    assert _hardcoded_paths("") == []


def test_directive_helper_returns_none_for_missing_key():
    assert _directive("[Service]\nType=oneshot\n", "Restart") is None
    assert _directive("", "Unit") is None


def test_directive_helper_returns_last_value_when_repeated():
    assert _directive("Unit=a.service\nUnit=b.service\n", "Unit") == "b.service"


def test_directive_helper_does_not_match_a_prefixed_key():
    # OnUnitActiveSec= must not be mistaken for Unit=.
    assert _directive("OnUnitActiveSec=60\n", "Unit") is None


def test_directive_helper_strips_surrounding_whitespace():
    assert _directive("Type= oneshot \n", "Type") == "oneshot"


def test_read_raises_filenotfounderror_naming_the_missing_path(tmp_path):
    missing = tmp_path / "does-not-exist.service.template"
    with pytest.raises(FileNotFoundError) as excinfo:
        _read(missing)
    assert str(missing) in str(excinfo.value)


def test_read_returns_exact_contents(tmp_path):
    path = tmp_path / "sample.timer.template"
    path.write_text("[Timer]\nOnUnitActiveSec=60\n", encoding="utf-8")
    assert _read(path) == "[Timer]\nOnUnitActiveSec=60\n"


def test_normalized_strips_trailing_newlines_and_crlf():
    assert _normalized("a\r\nb\n\n") == "a\nb"
    assert _normalized("") == ""
