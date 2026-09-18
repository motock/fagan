"""REG-5: the shipped launchd artifacts must carry no routing env vars.

model_registry.json is the single source of truth for role routing.  The
shipped scheduler template used to bake PIPELINE_BACKEND_* and
PIPELINE_LOCAL_MODEL_DEFAULT into its EnvironmentVariables block, which is how
a real incident happened: the plist pinned
PIPELINE_LOCAL_MODEL_DEFAULT=glm-5.3-flash:cloud while model_registry.json
pinned deepseek-v4.1-flash, so every diagnostic reported deepseek while every
agent ran glm.

RED state (the intended TDD state, not a bug in this suite): the routing keys
are still in launchd/com.fagan.pipeline.advance-scheduler.plist(.template),
scripts/dashboard.sh and scripts/install.sh still advertise them, and the files
that explain their environment block do not yet point at model_registry.json /
scripts/choose_providers.py.

House rules honoured here: the launchd env block is a SHARED artifact that
later stories may extend, so these tests assert MEMBERSHIP (survivors present,
routing keys absent) and never the block's total contents.  The mlx-supervisor
pair's pre-existing "--python" header comment is pinned verbatim by
tests/unit/test_generate_launchd_plists.py, so it is parsed with the same
comment-stripping oracle that suite uses rather than being graded here.
"""

from __future__ import annotations

import plistlib
import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LAUNCHD = _REPO_ROOT / "launchd"
_ADVANCE_TEMPLATE = _LAUNCHD / "com.fagan.pipeline.advance-scheduler.plist.template"
_ADVANCE_PLIST = _LAUNCHD / "com.fagan.pipeline.advance-scheduler.plist"
_ADVANCE_FILES = (_ADVANCE_TEMPLATE, _ADVANCE_PLIST)

_ROUTING_PREFIX = "PIPELINE_BACKEND_"
_ROUTING_EXACT = "PIPELINE_LOCAL_MODEL_DEFAULT"

# The routing keys the shipped scheduler env block carried.  These are the ONLY
# keys this story may remove from that block.
_ROUTING_KEYS = frozenset(
    {
        "PIPELINE_BACKEND_DISPATCH",
        "PIPELINE_BACKEND_PLANNER",
        "PIPELINE_BACKEND_REVIEW",
        "PIPELINE_LOCAL_MODEL_DEFAULT",
    }
)

# Every env key the advance-scheduler block carried BEFORE this story, minus the
# routing keys above.  Membership only: later stories may add keys.  This is the
# survivor list in its strongest available form -- a cleanup that drops any of
# these (a threshold, a resource gate, PATH/REPO_ROOT, a notify var) fails here.
_SURVIVOR_ENV_KEYS = (
    "LOCAL_AGENT_PARK_ENABLED",
    "LOCAL_AGENT_READ_HEAVY_DISTINCT_WINDOWS",
    "PATH",
    "PIPELINE_AUTONOMY",
    "PIPELINE_AUTO_ESCALATE",
    "PIPELINE_DECOMPOSE_SCRATCHPAD",
    "PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS",
    "PIPELINE_LOCAL_MAX_STEPS",
    "PIPELINE_LOCAL_TEMPERATURE",
    "PIPELINE_MAX_CONCURRENT_AGENTS",
    "PIPELINE_PAUSE_THRESHOLD",
    "PIPELINE_RESUME_THRESHOLD",
    "PIPELINE_REVIEW_MAX_TOKENS",
    "PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL",
    "PIPELINE_REWORK_MAX_ATTEMPTS",
    "PIPELINE_REWORK_MAX_ATTEMPTS_ESCALATED",
    "PIPELINE_REWORK_MAX_ATTEMPTS_ORACLE",
    "PIPELINE_REWORK_ON_CI_FAIL",
    "PIPELINE_WEEK_PAUSE_THRESHOLD",
    "PIPELINE_WEEK_RESUME_THRESHOLD",
    "REPO_ROOT",
)

# Program / working-directory keys: unrelated to routing, must survive.
_STRUCTURAL_KEYS = (
    "AbandonProcessGroup",
    "EnvironmentVariables",
    "KeepAlive",
    "Label",
    "ProgramArguments",
    "RunAtLoad",
    "StandardErrorPath",
    "StandardOutPath",
    "WorkingDirectory",
)

# Files that explain their environment block, so each must now say routing lives
# in model_registry.json and point at scripts/choose_providers.py.
_EXPLAINER_FILES = (
    _ADVANCE_TEMPLATE,
    _REPO_ROOT / "scripts" / "standalone-setup.sh",
    _REPO_ROOT / "scripts" / "dashboard.sh",
    _REPO_ROOT / "scripts" / "install.sh",
)

# Scripts that currently advertise the routing env vars as the way to configure
# routing.  scripts/standalone-setup.sh is deliberately NOT here: its
# PIPELINE_BACKEND_DISPATCH write is derived from model_registry.json and is
# pinned by tests/unit/test_standalone_setup_script.py.
_ADVERTISING_SCRIPTS = (
    _REPO_ROOT / "scripts" / "dashboard.sh",
    _REPO_ROOT / "scripts" / "install.sh",
)

_SMOKE = _REPO_ROOT / "scripts" / "smoke_getting_started.py"
_SHELL_SCRIPTS = tuple(sorted((_REPO_ROOT / "scripts").glob("*.sh")))


def _ids(paths):
    return [str(p.relative_to(_REPO_ROOT)) for p in paths]


def _launchd_files():
    return tuple(sorted(p for p in _LAUNCHD.rglob("*") if p.is_file()))


def _shipped_plists():
    return tuple(sorted(_LAUNCHD.glob("*.plist")) + sorted(_LAUNCHD.glob("*.plist.template")))


def _env_keys(path):
    return set(plistlib.loads(path.read_bytes())["EnvironmentVariables"])


def _routing_keys_in(keys):
    return sorted(k for k in keys if k.startswith(_ROUTING_PREFIX) or k == _ROUTING_EXACT)


# --------------------------------------------------------------------------- #
# 1. POSITIVE: no shipped launchd file mentions a routing env var at all
#    (the `grep -rn ... launchd/` done criterion, in-process).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", _launchd_files(), ids=_ids(_launchd_files()))
def test_no_launchd_file_mentions_routing_env_vars(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    hits = sorted(
        {
            line.strip()
            for line in text.splitlines()
            if _ROUTING_PREFIX in line or _ROUTING_EXACT in line
        }
    )
    assert hits == [], (
        f"{path.relative_to(_REPO_ROOT)} still mentions routing env vars "
        f"{hits!r}; routing now lives in model_registry.json alone, so the "
        "shipped launchd artifacts must not carry PIPELINE_BACKEND_* or "
        "PIPELINE_LOCAL_MODEL_DEFAULT"
    )


# --------------------------------------------------------------------------- #
# 2. POSITIVE: the scheduler env block itself carries no routing key
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", _ADVANCE_FILES, ids=_ids(_ADVANCE_FILES))
def test_advance_scheduler_env_block_has_no_routing_keys(path):
    keys = _env_keys(path)
    assert keys, f"{path.relative_to(_REPO_ROOT)} has an empty EnvironmentVariables block"
    assert _routing_keys_in(keys) == [], (
        f"{path.relative_to(_REPO_ROOT)} still pins routing keys "
        f"{_routing_keys_in(keys)!r} in EnvironmentVariables; delete them so "
        "the registry is the only source of truth"
    )


# --------------------------------------------------------------------------- #
# 3. NEGATIVE CONTROL: nothing but the routing keys may leave the env block
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", _ADVANCE_FILES, ids=_ids(_ADVANCE_FILES))
def test_only_routing_keys_may_be_removed_from_env_block(path):
    keys = _env_keys(path)
    missing = [k for k in _SURVIVOR_ENV_KEYS if k not in keys]
    assert missing == [], (
        f"{path.relative_to(_REPO_ROOT)} lost non-routing env keys {missing!r}; "
        "only PIPELINE_BACKEND_* and PIPELINE_LOCAL_MODEL_DEFAULT are routing "
        "(a false removal breaks the daemon)"
    )


# --------------------------------------------------------------------------- #
# 4. NEGATIVE CONTROL: the named survivors, asserted one by one
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", _SURVIVOR_ENV_KEYS)
def test_named_survivor_env_key_is_still_present(name):
    for path in _ADVANCE_FILES:
        assert name in _env_keys(path), (
            f"{name} is missing from {path.relative_to(_REPO_ROOT)}'s "
            "EnvironmentVariables block; it is not a routing variable and must "
            "survive this cleanup"
        )


@pytest.mark.parametrize("path", _ADVANCE_FILES, ids=_ids(_ADVANCE_FILES))
def test_pipeline_local_num_ctx_is_deliberately_removed_from_env_block(path):
    """This story removes PIPELINE_LOCAL_NUM_CTX on purpose, so the per-model
    _LOCAL_MODEL_TUNING table governs num_ctx for locally-dispatched models."""
    assert "PIPELINE_LOCAL_NUM_CTX" not in _env_keys(path), (
        f"{path.relative_to(_REPO_ROOT)} still pins PIPELINE_LOCAL_NUM_CTX in "
        "EnvironmentVariables; it must be removed so app/ollama_prompt_utils.py's "
        "per-model tuning table can govern num_ctx"
    )


@pytest.mark.parametrize("key", _STRUCTURAL_KEYS)
def test_structural_plist_key_is_still_present(key):
    for path in _ADVANCE_FILES:
        data = plistlib.loads(path.read_bytes())
        assert key in data, (
            f"{key} is missing from {path.relative_to(_REPO_ROOT)}; program and "
            "working-directory keys are not routing and must survive"
        )


# --------------------------------------------------------------------------- #
# 5. The operator-facing smoke guard is deliberate: do not touch it
# --------------------------------------------------------------------------- #
def test_smoke_getting_started_still_reads_the_dispatch_env_var():
    text = _SMOKE.read_text(encoding="utf-8")
    assert "PIPELINE_BACKEND_DISPATCH" in text, (
        "scripts/smoke_getting_started.py's PIPELINE_BACKEND_DISPATCH read is "
        "the operator-facing announce guard and is deliberately kept"
    )


# --------------------------------------------------------------------------- #
# 6. Every file that explains its env block points at the new source of truth
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", _EXPLAINER_FILES, ids=_ids(_EXPLAINER_FILES))
def test_env_block_explainer_points_at_the_registry(path):
    text = path.read_text(encoding="utf-8")
    assert "model_registry.json" in text, (
        f"{path.relative_to(_REPO_ROOT)} explains its environment block but "
        "never says routing now lives in model_registry.json"
    )
    assert "choose_providers.py" in text, (
        f"{path.relative_to(_REPO_ROOT)} explains its environment block but "
        "does not point at scripts/choose_providers.py"
    )


@pytest.mark.parametrize("path", _ADVERTISING_SCRIPTS, ids=_ids(_ADVERTISING_SCRIPTS))
def test_setup_scripts_no_longer_advertise_routing_env_vars(path):
    text = path.read_text(encoding="utf-8")
    hits = sorted(
        {
            line.strip()
            for line in text.splitlines()
            if _ROUTING_PREFIX in line or _ROUTING_EXACT in line
        }
    )
    assert hits == [], (
        f"{path.relative_to(_REPO_ROOT)} still advertises routing env vars "
        f"{hits!r}; point operators at model_registry.json / "
        "scripts/choose_providers.py instead"
    )


# --------------------------------------------------------------------------- #
# 7. Every shipped plist still parses (plistlib rejects '--' in a comment)
# --------------------------------------------------------------------------- #
# The mlx-supervisor pair carries a pre-existing header comment whose literal
# "--python" is pinned VERBATIM by tests/unit/test_generate_launchd_plists.py
# (test_mlx_supervisor_template_preserves_explanatory_comment_verbatim), so it
# cannot be de-double-hyphened without breaking an existing test.  Those two
# files are therefore parsed with the same comment-stripping oracle the
# existing suite uses; every OTHER shipped plist must parse with raw plistlib,
# which is what stops a newly added comment from using '--'.
_COMMENT_QUIRK_FILES = frozenset(
    {
        "com.fagan.pipeline.mlx-supervisor.plist",
        "com.fagan.pipeline.mlx-supervisor.plist.template",
    }
)
_XML_COMMENT_RE = re.compile(rb"<!--.*?-->", re.DOTALL)


@pytest.mark.parametrize("path", _shipped_plists(), ids=_ids(_shipped_plists()))
def test_shipped_plist_parses(path):
    raw = path.read_bytes()
    if path.name in _COMMENT_QUIRK_FILES:
        # Structure must survive; the comment quirk is pre-existing and pinned.
        plistlib.loads(_XML_COMMENT_RE.sub(b"", raw))
        return
    try:
        plistlib.loads(raw)
    except Exception as exc:
        raise AssertionError(
            f"{path.relative_to(_REPO_ROOT)} no longer parses as a plist: "
            f"{type(exc).__name__}: {exc}. plistlib rejects a '--' sequence "
            "inside an XML comment, so a comment added here must not use "
            "double hyphens."
        ) from exc


@pytest.mark.parametrize("path", _ADVANCE_FILES, ids=_ids(_ADVANCE_FILES))
def test_advance_scheduler_plist_parses_without_comment_stripping(path):
    """The scheduler pair must stay parseable by raw plistlib, so any comment
    this story adds to it must avoid the '--' sequence."""
    plistlib.loads(path.read_bytes())


def test_plistlib_rejects_double_hyphen_inside_a_comment():
    """Boundary: documents why a plist comment must avoid '--'."""
    bad = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0">\n'
        b"<!-- routing lives in model_registry.json -- see choose_providers.py -->\n"
        b"<dict><key>Label</key><string>x</string></dict>\n</plist>\n"
    )
    with pytest.raises(Exception) as excinfo:
        plistlib.loads(bad)
    assert "not well-formed" in str(excinfo.value)


def test_plistlib_rejects_malformed_bytes():
    with pytest.raises(plistlib.InvalidFileException):
        plistlib.loads(b"this is not a plist at all")


def test_routing_key_detector_is_not_vacuous():
    """The detector must actually see a routing key, and stay quiet on an
    empty env block (boundary: zero keys)."""
    with_routing = plistlib.dumps(
        {"EnvironmentVariables": {"PIPELINE_BACKEND_DISPATCH": "local", "PATH": "/bin"}}
    )
    keys = set(plistlib.loads(with_routing)["EnvironmentVariables"])
    assert _routing_keys_in(keys) == ["PIPELINE_BACKEND_DISPATCH"]
    assert _routing_keys_in(set()) == []
    assert _routing_keys_in({"PIPELINE_LOCAL_MODEL_DEFAULT"}) == [
        "PIPELINE_LOCAL_MODEL_DEFAULT"
    ]


# --------------------------------------------------------------------------- #
# 8. bash -n passes on the shipped shell scripts
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", _SHELL_SCRIPTS, ids=_ids(_SHELL_SCRIPTS))
def test_shell_script_passes_bash_n(path):
    proc = subprocess.run(
        ["bash", "-n", str(path)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, (
        f"bash -n {path.relative_to(_REPO_ROOT)} failed:\n{proc.stderr}"
    )


def test_bash_n_negative_control(tmp_path):
    """The bash -n probe above must be able to fail."""
    broken = tmp_path / "broken.sh"
    broken.write_text("if [ 1 -eq 1 ]; then\n  echo hi\n", encoding="utf-8")
    proc = subprocess.run(
        ["bash", "-n", str(broken)], capture_output=True, text=True, check=False
    )
    assert proc.returncode != 0
