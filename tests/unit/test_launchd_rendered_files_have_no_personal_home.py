"""The four committed generator-output files must not leak a personal home.

launchd/com.fagan.pipeline.{advance-scheduler,mlx-supervisor,usage-poller}.plist
and launchd/pipeline-logs.newsyslog.conf are rendered output committed beside
their .template files. They used to carry the value of $HOME on the
maintainer's machine - personal data in a public repo - and this story
rewrites every occurrence of it to the anonymous home /Users/example.

The running pipeline reads ~/Library/LaunchAgents/ and never these repo
copies, so the rewrite has no runtime effect; the six tests that read these
files are the regression check that the rewrite stays mechanical.

The strings this suite rejects are built at run time so that this file does
not itself contain the literal it forbids:
  * str(Path.home()) - the home of whatever host runs the suite;
  * "/Users/" + "jesse" + "carroll" - the home baked into the committed
    files, assembled from parts so the check still bites on a CI host whose
    own home is somewhere else entirely.
"""
import plistlib
import re
from pathlib import Path

_LAUNCHD = Path(__file__).resolve().parent.parent.parent / "launchd"

# Exactly the four files this story rewrites - nothing else may change.
RENDERED_FILES = (
    "com.fagan.pipeline.advance-scheduler.plist",
    "com.fagan.pipeline.mlx-supervisor.plist",
    "com.fagan.pipeline.usage-poller.plist",
    "pipeline-logs.newsyslog.conf",
)
PLIST_FILES = ("com.fagan.pipeline.advance-scheduler.plist",
               "com.fagan.pipeline.mlx-supervisor.plist",
               "com.fagan.pipeline.usage-poller.plist")
# The plists whose Standard{Out,Error}Path logs the newsyslog conf rotates.
_ROTATED_PLISTS = ("com.fagan.pipeline.advance-scheduler.plist",
                   "com.fagan.pipeline.usage-poller.plist")

_ANONYMOUS_USER = "example"
_ANONYMOUS_HOME = "/Users/" + _ANONYMOUS_USER
_RUNTIME_HOME = str(Path.home())
_LITERAL_HOME = "/Users/" + "jesse" + "carroll"
_USERNAME = "jesse" + "carroll"

# newsyslog's fixed field order: logfilename mode count size when flags.
_NEWSYSLOG_FIELDS = 6
_XML_COMMENT_RE = re.compile(rb"<!--.*?-->", re.DOTALL)


def _path(name):
    path = _LAUNCHD / name
    assert path.is_file(), f"missing {path}"
    return path


def _text(name):
    return _path(name).read_text()


def _plist(name):
    """Parse a committed plist with plistlib.loads.

    XML comments are stripped first because the committed mlx-supervisor.plist
    documents its venv with "uv venv --python", and "--" is illegal inside an
    XML comment, so expat rejects that file's raw bytes. This is the same
    convention as test_acceptance_launchd_plist_portability.py, and it keeps
    this story from being pushed into editing that comment.
    """
    raw = _path(name).read_bytes()
    return plistlib.loads(_XML_COMMENT_RE.sub(b"", raw))


def test_all_four_rendered_files_are_present():
    for name in RENDERED_FILES:
        _path(name)


def test_rendered_files_reference_the_anonymous_home():
    for name in RENDERED_FILES:
        text = _text(name)
        assert _ANONYMOUS_HOME in text, (
            f"{name} must reference the anonymous home {_ANONYMOUS_HOME}"
        )
        # Every baked-in occurrence is a path prefix, so the anonymous home
        # must appear followed by a separator - catches a replacement that
        # landed as a bare token or with a doubled separator.
        assert _ANONYMOUS_HOME + "/" in text, (
            f"{name} must use {_ANONYMOUS_HOME} as a path prefix"
        )
        assert _ANONYMOUS_HOME + "//" not in text, (
            f"{name} has a doubled separator after {_ANONYMOUS_HOME}"
        )


def test_rendered_files_contain_no_personal_home():
    for name in RENDERED_FILES:
        text = _text(name)
        for bad in (_RUNTIME_HOME, _LITERAL_HOME):
            assert bad not in text, (
                f"{name} still contains a personal home path"
            )
        # Catches a partial rewrite that leaves the username behind in some
        # other position, e.g. as "/Users/example<username>".
        assert _USERNAME not in text, (
            f"{name} still contains the maintainer's username"
        )


def test_rendered_files_have_no_unresolved_template_placeholder():
    """The rendered files are generator OUTPUT, not the templates themselves.

    Cuts off the tempting shortcut of copying a .template over the rendered
    file (or hand-editing one into the other's shape) instead of doing the
    mechanical home rewrite: an unsubstituted {{...}} token in a file that
    launchd/newsyslog would read verbatim is a silent misconfiguration.
    """
    for name in RENDERED_FILES:
        text = _text(name)
        assert "{{" not in text, (
            f"{name} still contains an unresolved template placeholder"
        )
        assert "}}" not in text, f"{name} contains a stray template delimiter"


def test_rendered_plists_still_parse():
    for name in PLIST_FILES:
        data = _plist(name)
        assert isinstance(data, dict), f"{name} did not parse to a dict"
        assert "Label" in data, f"{name} lost its Label key"


def test_newsyslog_conf_still_rotates_the_plists_logs():
    entries = [
        line.split()
        for line in _text("pipeline-logs.newsyslog.conf").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert entries, "pipeline-logs.newsyslog.conf has no rotation entries"
    for fields in entries:
        assert len(fields) == _NEWSYSLOG_FIELDS, (
            f"malformed newsyslog entry (want {_NEWSYSLOG_FIELDS} fields): "
            f"{fields}"
        )
        assert fields[0].startswith(_ANONYMOUS_HOME + "/"), (
            f"rotation entry is not under the anonymous home: {fields[0]}"
        )
    rotated = {fields[0] for fields in entries}
    for name in _ROTATED_PLISTS:
        data = _plist(name)
        for key in ("StandardOutPath", "StandardErrorPath"):
            assert data[key] in rotated, (
                f"{name} {key} has no rotation entry in the newsyslog conf"
            )


def test_no_personal_home_in_any_launchd_file():
    """The story's goal is a public repo with no personal home in launchd/.

    Scoped to launchd/ rather than the whole tree: docs/plans and the local
    virtualenv legitimately mention the maintainer's path and are out of
    scope for this story.
    """
    for path in sorted(_LAUNCHD.iterdir()):
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")
        for bad in (_RUNTIME_HOME, _LITERAL_HOME):
            assert bad not in text, f"{path.name} contains a personal home"