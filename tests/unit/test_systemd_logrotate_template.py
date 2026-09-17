"""Acceptance fixture: the Linux/systemd counterpart of the launchd newsyslog
rotation config.

`systemd/pipeline-logs.logrotate.template` is the logrotate equivalent of
`launchd/pipeline-logs.newsyslog.conf.template`: systemd does not rotate a
unit's StandardOutput/StandardError append-mode log files, so an unattended
overnight system can accumulate stderr over weeks. The template is a
placeholder file ({{REPO_ROOT}} is substituted by a later generator story),
so it must NOT be installed as-is and must NOT carry a per-user {{HOME}} token.

The `copytruncate` directive is load-bearing: neither advance-scheduler nor
usage-poller re-opens its log file handle after rotation, so the file must be
truncated in place rather than renamed (the same constraint the newsyslog
template encodes with its `N = do not signal any process` flag). It is graded
explicitly as a regression guard.

On current master this file FAILS: systemd/ and the template do not exist yet.
"""
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SYSTEMD = _REPO / "systemd"
_TEMPLATE = _SYSTEMD / "pipeline-logs.logrotate.template"
_LAUNCHD_NEWSYSLOG = _REPO / "launchd" / "pipeline-logs.newsyslog.conf.template"

_PLACEHOLDER = "{{REPO_ROOT}}"
_LOG_NAMES = (
    "advance-scheduler.log",
    "advance-scheduler.err.log",
    "usage-poller.log",
    "usage-poller.err.log",
)
_EXPECTED_PATHS = tuple(f"{_PLACEHOLDER}/{name}" for name in _LOG_NAMES)

# The exact content the story specifies, verbatim ({{REPO_ROOT}} left literal).
_EXPECTED_TEXT = """\
# logrotate config for the pipeline's systemd unit logs - the Linux
# equivalent of launchd/pipeline-logs.newsyslog.conf.template. See that
# file's comment for why rotation matters (an unattended overnight system
# can accumulate stderr over weeks; systemd does not rotate a unit's
# StandardOutput/StandardError append-mode log files on its own).
#
# Install (one-time, needs sudo):
#   sudo cp systemd/pipeline-logs.logrotate.conf /etc/logrotate.d/com.fagan.pipeline
# logrotate normally runs daily via cron or systemd-logrotate.timer; force a
# pass with:  sudo logrotate -f /etc/logrotate.d/com.fagan.pipeline
{{REPO_ROOT}}/advance-scheduler.log
{{REPO_ROOT}}/advance-scheduler.err.log
{{REPO_ROOT}}/usage-poller.log
{{REPO_ROOT}}/usage-poller.err.log
{
    rotate 5
    size 5M
    compress
    missingok
    notifempty
    copytruncate
}
"""

_BLOCK_DIRECTIVES = (
    "rotate 5",
    "size 5M",
    "compress",
    "missingok",
    "notifempty",
    "copytruncate",
)


def _read(path: Path) -> str:
    assert path.is_file(), f"missing {path}"
    return path.read_text(encoding="utf-8")


def _path_lines(text: str) -> list[str]:
    """Uncommented lines that name a log file (the rotation targets)."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and line.endswith(".log"):
            out.append(line)
    return out


def _problems(text: str) -> list[str]:
    """Return every way `text` fails to be a valid template (empty == valid).

    Kept as a pure function so the negative cases below can prove the checks
    actually fire instead of passing vacuously.
    """
    problems: list[str] = []
    if not text.strip():
        return ["template is empty"]

    paths = _path_lines(text)
    for expected in _EXPECTED_PATHS:
        if expected not in paths:
            problems.append(f"missing log path line {expected!r}")
    if len(paths) != len(_EXPECTED_PATHS):
        problems.append(f"expected {len(_EXPECTED_PATHS)} log path lines, got {len(paths)}")
    if len(set(paths)) != len(paths):
        problems.append("duplicate log path lines")

    if text.count(_PLACEHOLDER) != len(_EXPECTED_PATHS):
        problems.append(f"{_PLACEHOLDER} must appear exactly {len(_EXPECTED_PATHS)} times")
    if "{{HOME}}" in text:
        problems.append("must not contain the {{HOME}} token")
    if re.search(r"\{\{(?!REPO_ROOT\}\})[A-Z_]+\}\}", text):
        problems.append("contains an unexpected placeholder token")
    if "/Users/" in text or "$HOME" in text:
        problems.append("contains a substituted per-user path")

    for directive in _BLOCK_DIRECTIVES:
        if directive not in text:
            problems.append(f"missing directive {directive!r}")
    if "copytruncate" not in text:
        problems.append("missing copytruncate (log handle is never re-opened)")

    body = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#") and not ln.strip().endswith(".log")
    ]
    if body != ["{", *_BLOCK_DIRECTIVES, "}"]:
        problems.append(f"unexpected rotation block: {body!r}")
    return problems


def test_template_file_exists():
    assert _SYSTEMD.is_dir(), f"missing directory {_SYSTEMD}"
    assert _TEMPLATE.is_file(), f"missing {_TEMPLATE}"


def test_systemd_is_sibling_of_launchd():
    assert _SYSTEMD.parent == _REPO, "systemd/ must live at the repo root"
    assert (_REPO / "launchd").is_dir(), "launchd/ must remain a sibling of systemd/"


def test_template_content_is_verbatim():
    text = _read(_TEMPLATE)
    # Trailing-newline convention is not part of the spec, so normalize it
    # away; everything else must match the story's content byte-for-byte.
    assert text.rstrip("\n") == _EXPECTED_TEXT.rstrip("\n"), (
        "template content must match the story verbatim"
    )
    assert text.strip().endswith("}"), "template must end with the closing brace"


def test_all_four_log_paths_use_repo_root_placeholder():
    text = _read(_TEMPLATE)
    paths = _path_lines(text)
    for expected in _EXPECTED_PATHS:
        assert expected in paths, f"missing literal path line {expected!r}"
    assert len(paths) == len(_EXPECTED_PATHS), f"expected exactly 4 log paths, got {paths!r}"
    assert text.count(_PLACEHOLDER) == len(_EXPECTED_PATHS)


def test_placeholder_is_literal_not_substituted():
    text = _read(_TEMPLATE)
    assert "/Users/" not in text, "template must not bake in a personal path"
    assert "$HOME" not in text, "template must not use shell expansion"
    assert re.search(r"\{\{(?!REPO_ROOT\}\})[A-Z_]+\}\}", text) is None


def test_contains_copytruncate():
    """Regression guard: the log handle is never re-opened after rotation."""
    text = _read(_TEMPLATE)
    assert "copytruncate" in text, "copytruncate is required (no process re-opens the log)"
    assert "copytruncate" in _BLOCK_DIRECTIVES
    assert "copytruncate" in [ln.strip() for ln in text.splitlines()]


def test_rotate_5_matches_newsyslog_retention():
    text = _read(_TEMPLATE)
    assert "rotate 5" in text, "logrotate must keep 5 rotated files"
    newsyslog = _read(_LAUNCHD_NEWSYSLOG)
    counts = {
        fields[2]
        for line in newsyslog.splitlines()
        if not line.strip().startswith("#") and line.strip().endswith("NJ")
        for fields in [line.split()]
        if len(fields) >= 3
    }
    assert counts == {"5"}, f"newsyslog retention drifted: {counts!r}"


def test_rotation_directives_present():
    text = _read(_TEMPLATE)
    for directive in _BLOCK_DIRECTIVES:
        assert directive in text, f"missing directive {directive!r}"


def test_does_not_contain_home_token():
    text = _read(_TEMPLATE)
    assert "{{HOME}}" not in text, "this template has no per-user path, unlike the unit templates"


def test_install_comment_names_logrotate_d_target():
    text = _read(_TEMPLATE)
    assert "/etc/logrotate.d/com.fagan.pipeline" in text
    assert "sudo cp systemd/pipeline-logs.logrotate.conf" in text
    assert "launchd/pipeline-logs.newsyslog.conf.template" in text, "cross-reference the macOS template"


def test_validator_accepts_the_real_template():
    assert _problems(_read(_TEMPLATE)) == []


def test_validator_rejects_malformed_inputs():
    """Negative cases: prove each check fires, so the suite is not vacuous."""
    assert _problems("") == ["template is empty"]
    assert _problems("   \n\n") == ["template is empty"]

    assert any("copytruncate" in p for p in _problems(_EXPECTED_TEXT.replace("    copytruncate\n", "")))
    assert any("rotate 5" in p for p in _problems(_EXPECTED_TEXT.replace("rotate 5", "rotate 3")))
    assert any("{{HOME}}" in p for p in _problems(_EXPECTED_TEXT + "{{HOME}}/x.log\n"))
    assert any("per-user path" in p for p in _problems(_EXPECTED_TEXT.replace(_PLACEHOLDER, "/Users/x")))
    assert any("missing log path line" in p for p in _problems(_EXPECTED_TEXT.replace("usage-poller.log\n", "")))
    assert any("unexpected placeholder" in p for p in _problems(_EXPECTED_TEXT + "{{OTHER}}/x.log\n"))
    assert any("duplicate" in p for p in _problems(_EXPECTED_TEXT + f"{_PLACEHOLDER}/usage-poller.log\n"))


def test_launchd_newsyslog_template_is_untouched():
    """The macOS artifact must keep working; this story only adds systemd/."""
    text = _read(_LAUNCHD_NEWSYSLOG)
    for expected in _EXPECTED_PATHS:
        assert expected in text, f"launchd newsyslog template lost {expected!r}"
    assert " NJ" in text, "newsyslog 'do not signal any process' flag must remain"
