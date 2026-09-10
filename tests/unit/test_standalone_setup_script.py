"""Structural wiring tests for scripts/standalone-setup.sh (standalone provisioning).

The story adds ONE supported command that provisions a standalone instance and
REFUSES to report success unless the dashboard and the scheduler agree::

    scripts/standalone-setup.sh up | down | status
        [--repo-root DIR] [--data-dir DIR] [--target-repo DIR]
        [--port PORT] [--autonomy MODE] [--force]

House rules honoured here (mirrors tests/unit/test_install_script_wiring.py):

* the script is never executed beyond ``bash -n``: no dashboard is started, no
  scheduler is spawned, no HTTP request is made and no data directory is
  created -- this is a unit suite over the script's SOURCE TEXT;
* assertions are membership/ordering based against fixed anchors, never on the
  file's total contents, line count or a hash;
* scripts/dashboard.sh and scripts/scheduler.sh are shared artifacts this
  story must NOT modify, so they are only probed for existence (loudly), and
  no test pins their contents.

Contract pinned here for the implementer (each assertion below is a contract
line; satisfy them in the cheapest way that keeps the script correct):

* subcommands dispatch as ``up)`` / ``down)`` / ``status)`` case branches (or
  ``cmd_up()``-style functions, or ``[ "$1" = "up" ]`` tests);
* the shared env file is ``.pipeline.env``; the keys PLAN_DIR, WORKTREE_ROOT
  and PIPELINE_AUTONOMY are written as one adjacent block whose lines carry no
  tilde (the file is sourced with ``set -a`` and gets no tilde expansion), and
  the data dir is absolutised (``cd ... && pwd``, ``readlink -f`` or
  ``realpath``) before those values are written;
* defaults: port 8001 (never 8000), data dir ``~/pipeline-standalone``
  (HOME-anchored), autonomy ``dry-run``;
* the /api/health verification polls with retries + sleep, sends the
  x-pipeline-api-key header (key from ``app.auth.get_or_create_api_key``),
  asserts ``plan_dir`` equals the intended path, and treats a non-empty
  ``config_mismatch`` as fatal: non-zero exit, message naming the diverging
  fields, and NO success banner in that branch;
 * ``down`` stops both processes and says the scratch data stays put;
   ``status`` prints the resolved paths and both processes' state.
 * ``up`` derives PIPELINE_BACKEND_DISPATCH from model_registry.json's
   ``.roles.dispatch.provider`` (jq if available, grep/sed or the existing
   ``$VENV_PY -c`` style as fallback) and writes it -- quoted, like every
   value here -- into the SAME brace block, AFTER the PIPELINE_AUTONOMY line
   and BEFORE ``} >"$ENV_FILE"``; the write is guarded by a set-u-safe
   ``${PIPELINE_BACKEND_DISPATCH:-}`` emptiness test so an operator's own
   exported value always wins, and a missing/unreadable/provider-less
   registry silently writes nothing extra (fail open -- no die/exit near the
   derivation); one ``==> `` status line says which of the three outcomes
   happened (registry / operator's env / unset).  The env-first resolution
   in pipeline/dispatch.py is out of scope and must not change.

Status: scripts/standalone-setup.sh exists and every test in this module
passes (the two review-regression probes at the bottom were RED when added
and went GREEN once the script's cwd-independent key lookup and `down`
fail-loud behaviour landed).
"""

import re
import subprocess
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "standalone-setup.sh"

_DASHBOARD_SH = "scripts/dashboard.sh"
_SCHEDULER_SH = "scripts/scheduler.sh"
_ENV_FILE = ".pipeline.env"
_ENV_KEYS = ("PLAN_DIR", "WORKTREE_ROOT", "PIPELINE_AUTONOMY")
_OPTIONS = (
    "--repo-root",
    "--data-dir",
    "--target-repo",
    "--port",
    "--autonomy",
    "--force",
)

# Dependencies from earlier stories; fail loudly (not skip) if they vanished,
# so a broken dependency is never silently green.
for _dep in (_DASHBOARD_SH, _SCHEDULER_SH):
    if not (_REPO_ROOT / _dep).exists():
        raise ImportError(
            f"TDD dependency missing: {_dep} does not exist. "
            "standalone-setup.sh launches both of them."
        )


# --------------------------------------------------------------------------- #
# helpers (source-text probes only -- the script is never executed)
# --------------------------------------------------------------------------- #
def _require_script():
    if not _SCRIPT.exists():
        pytest.fail(
            f"TDD RED state: {_SCRIPT} does not exist yet. "
            "scripts/standalone-setup.sh is the implementation this suite "
            "drives; write it (see this module's docstring for the contract)."
        )


def _source():
    _require_script()
    return _SCRIPT.read_text(encoding="utf-8")


def _code_lines(source):
    """Non-blank, non-comment lines (inline comments after code stay)."""
    return [
        ln
        for ln in source.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


def _code(source):
    return "\n".join(_code_lines(source))


def _line_containing(source, *needles):
    """First code line containing all needles, else None."""
    for ln in _code_lines(source):
        if all(needle in ln for needle in needles):
            return ln
    return None


def _line_index(source, *needles):
    """Index (into code lines) of the first line containing all needles."""
    for i, ln in enumerate(_code_lines(source)):
        if all(needle in ln for needle in needles):
            return i
    return None


def _find(source, pattern):
    """(index, line) of the first code line matching regex, else (None, None)."""
    for i, ln in enumerate(_code_lines(source)):
        if re.search(pattern, ln):
            return i, ln
    return None, None


def _dispatch_line_index(lines, name):
    """Index of the line that dispatches subcommand `name`, else None."""
    case_pat = re.compile(r"^\s*" + name + r"\)")
    func_pat = re.compile(r"^\s*(?:cmd_|do_)?" + name + r"\s*\(\)")
    test_pat = re.compile(
        r'\[\s*"\$\{?[A-Za-z_0-9]+(?::-[^}]*)?\}?"\s*=\s*"' + name + r'"\s*\]'
    )
    for i, ln in enumerate(lines):
        if case_pat.match(ln) or func_pat.match(ln) or test_pat.search(ln):
            return i
    return None


def _dispatches_subcommand(source, name):
    return _dispatch_line_index(_code_lines(source), name) is not None


def _subcommand_region(source, name, span=30):
    """Code lines from the subcommand's dispatch line onward (bounded)."""
    lines = _code_lines(source)
    i = _dispatch_line_index(lines, name)
    if i is None:
        return []
    return lines[i : i + span]


def _mismatch_region(source, span=30):
    """Code lines from the first config_mismatch mention onward (bounded)."""
    idx = _line_index(source, "config_mismatch")
    if idx is None:
        return []
    return _code_lines(source)[idx : idx + span]


def _is_banner_line(ln):
    """A success-banner-looking output line (echo/printf of success/ready)."""
    low = ln.lower()
    looks_success = (
        "==>" in ln
        or "success" in low
        or "ready" in low
        or "is up" in low
    )
    is_output = re.search(r"\b(?:echo|printf|cat|log|say)\b", ln) is not None
    return looks_success and is_output


def _data_dir_var(source):
    """The variable name that holds the default data dir, if discoverable."""
    for ln in _code_lines(source):
        if "pipeline-standalone" in ln:
            m = re.search(r"([A-Z_][A-Z_0-9]*)\s*=", ln)
            if m:
                return m.group(1)
    return None


# --------------------------------------------------------------------------- #
# (a) existence, syntax, subcommands, options
# --------------------------------------------------------------------------- #
def test_script_exists_and_is_executable():
    """The entrypoint exists, is executable and has a bash shebang."""
    _require_script()
    mode = _SCRIPT.stat().st_mode
    assert mode & 0o111, (
        f"{_SCRIPT} must be executable (chmod +x); got mode {oct(mode)}"
    )
    first = _SCRIPT.read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("#!"), "missing shebang line"
    assert "bash" in first, f"shebang must use bash, got {first!r}"


def test_bash_n_parses_the_script_cleanly():
    """NEGATIVE: `bash -n scripts/standalone-setup.sh` exits 0."""
    _require_script()
    proc = subprocess.run(
        ["bash", "-n", str(_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"bash -n rejected scripts/standalone-setup.sh "
        f"(exit {proc.returncode}):\n{proc.stderr}"
    )


def test_script_defines_up_down_and_status_subcommands():
    """up, down and status are each dispatched by the script."""
    src = _source()
    missing = [
        name
        for name in ("up", "down", "status")
        if not _dispatches_subcommand(src, name)
    ]
    assert not missing, f"subcommands not dispatched: {missing}"


def test_script_declares_all_documented_options():
    """--repo-root/--data-dir/--target-repo/--port/--autonomy/--force all parsed."""
    src = _source()
    code = _code(src)
    missing = [opt for opt in _OPTIONS if opt not in code]
    assert not missing, f"documented options missing from the parser: {missing}"


def test_script_runs_under_set_e_strict_mode():
    """A provisioning entrypoint must fail loudly: `set -e` (or stricter)."""
    assert re.search(r"(?m)^\s*set\s+-[a-zA-Z]*e", _source()), (
        "the script must enable errexit (e.g. `set -euo pipefail`)"
    )


# --------------------------------------------------------------------------- #
# (b) wiring: dashboard.sh, scheduler.sh, .pipeline.env, no MCP
# --------------------------------------------------------------------------- #
def test_script_references_dashboard_scheduler_and_env_file():
    """All three wiring anchors appear on code lines (not just comments)."""
    src = _source()
    for literal in (_DASHBOARD_SH, _SCHEDULER_SH, _ENV_FILE):
        line = _line_containing(src, literal)
        assert line is not None, f"missing code reference to {literal!r}"


def test_env_file_is_written_before_both_processes_are_launched():
    """Ordering: .pipeline.env is written before dashboard.sh/scheduler.sh run,
    so both pick up the shared env file (keep the script linear)."""
    src = _source()
    env_idx = _line_index(src, _ENV_FILE)
    dash_idx = _line_index(src, _DASHBOARD_SH)
    sched_idx = _line_index(src, _SCHEDULER_SH)
    assert None not in (env_idx, dash_idx, sched_idx), "wiring anchors missing"
    assert env_idx < dash_idx, (
        ".pipeline.env must be written before scripts/dashboard.sh is launched"
    )
    assert env_idx < sched_idx, (
        ".pipeline.env must be written before scripts/scheduler.sh is launched"
    )


def test_no_mcp_registration():
    """NEGATIVE: standalone means no MCP registration, ever."""
    src = _source()
    assert "claude mcp add" not in src, (
        "standalone setup must not register an MCP server (`claude mcp add`)"
    )
    assert "mcp add" not in _code(src), (
        "no `mcp add` invocation may appear on a code line"
    )


# --------------------------------------------------------------------------- #
# (c) up: repo root, venv resolution, data dirs, scratch repo
# --------------------------------------------------------------------------- #
def test_up_resolves_repo_root_from_bash_source():
    """The repo root is resolved from BASH_SOURCE (install.sh idiom) and
    --repo-root is an accepted override."""
    src = _source()
    root_line = _line_containing(src, "BASH_SOURCE", "dirname")
    assert root_line is not None, (
        "repo root must be resolved from BASH_SOURCE (like scripts/install.sh)"
    )
    assert "--repo-root" in _code(src), "--repo-root must be accepted"


def test_up_resolves_the_venv_python():
    """The venv interpreter (not bare `python3`) is what the script drives."""
    src = _source()
    assert _line_containing(src, ".venv") is not None, ".venv must be resolved"
    assert _line_containing(src, "bin/python") is not None, (
        "the venv interpreter (<venv>/bin/python3) must be resolved"
    )


def test_up_fails_actionably_when_venv_is_missing():
    """No .venv -> non-zero exit with a message naming scripts/install.sh."""
    src = _source()
    idx, guard = _find(
        src,
        r"\.venv.*(-x|-d|-f)|(-x|-d|-f)[^\n]*\"\$\{?(PYBIN|VENV)",
    )
    assert guard is not None, (
        "the script must existence-test the .venv (or the venv python) before "
        "launching anything"
    )
    assert "scripts/install.sh" in src, (
        "the no-venv error message must name scripts/install.sh"
    )
    window = _code_lines(src)[max(0, idx - 2) : idx + 6]
    assert any(
        re.search(r"\bexit\s+[1-9]\b|\bdie\b", ln) for ln in window
    ), (
        "the missing-.venv branch must exit non-zero with an actionable message"
    )


def test_up_creates_plans_and_worktrees_directories():
    """<data-dir>/plans and <data-dir>/worktrees are mkdir -p'd."""
    src = _source()
    for name in ("plans", "worktrees"):
        line = _line_containing(src, "mkdir", name)
        assert line is not None, (
            f"`up` must mkdir the {name!r} directory under the data dir"
        )


def test_up_creates_agents_directory():
    """<data-dir>/agents is mkdir -p'd, alongside plans and worktrees."""
    src = _source()
    line = _line_containing(src, "mkdir", "agents")
    assert line is not None, (
        "`up` must mkdir the 'agents' directory under the data dir"
    )


def test_up_provisions_agents_dir_from_repo_bundled_personas_non_destructively():
    """The repo's bundled agents/*.md personas are copied into the
    provisioned AGENTS_DIR, non-destructively (so a re-run never clobbers an
    operator's already-customized personas), guarded on the source directory
    existing - mirrors the README's manual `cp agents/*.md ~/.claude/agents/`
    step, but scoped under DATA_DIR so a fresh install never depends on the
    operator's global ~/.claude/agents/ already being populated (fixes:
    every dispatch_story call failing with FileNotFoundError: No persona
    named ... on a genuinely fresh machine, regardless of dispatch
    backend)."""
    src = _source()
    cp_idx, cp_line = _find(src, r"\bcp\b.*agents.*\.md")
    assert cp_line is not None, (
        "`up` must copy the repo's bundled agents/*.md persona files into "
        "the provisioned AGENTS_DIR"
    )
    assert "-n" in cp_line or "--no-clobber" in cp_line, (
        "the persona copy must be non-destructive (cp -n / --no-clobber) so "
        f"a re-run of `up` never overwrites a customized persona: {cp_line!r}"
    )
    lines = _code_lines(src)
    guard_context = lines[max(0, cp_idx - 3) : cp_idx]
    assert any(re.search(r"\bif\b|\[ -d", ln) for ln in guard_context), (
        "the persona copy must be guarded by an existence test on the "
        "repo's agents/ source directory"
    )


def test_up_creates_scratch_repo_only_when_target_repo_absent():
    """A scratch git repo is git-inited at <data-dir>/repo, guarded on
    --target-repo being unset."""
    src = _source()
    git_idx = _line_index(src, "git init")
    assert git_idx is not None, (
        "`up` must create a scratch git repo (`git init`) when --target-repo "
        "is not supplied"
    )
    lines = _code_lines(src)
    scratch_at_repo = (
        _line_containing(src, "git init", "repo") is not None
        or _line_containing(src, "mkdir", "repo") is not None
    )
    assert scratch_at_repo, "the scratch repo must live at <data-dir>/repo"
    target_idx = _line_index(src, "--target-repo")
    assert target_idx is not None, "--target-repo must be parsed"
    assert target_idx < git_idx, (
        "--target-repo must be handled before the scratch repo is created"
    )
    guard_context = lines[max(0, git_idx - 4) : git_idx]
    assert any(
        re.search(r"\bif\b|\[ -z|\[ -n|:-|\belse\b", ln) for ln in guard_context
    ), "scratch repo creation must be guarded on --target-repo being unset"


# --------------------------------------------------------------------------- #
# (d) the CFG-D1 shared env file contract
# --------------------------------------------------------------------------- #
def test_env_file_writes_plan_dir_worktree_root_and_autonomy():
    """PLAN_DIR, WORKTREE_ROOT and PIPELINE_AUTONOMY are written into
    .pipeline.env as one adjacent block."""
    src = _source()
    lines = _code_lines(src)
    idxs = []
    for key in _ENV_KEYS:
        idx = None
        for i, ln in enumerate(lines):
            if f"{key}=" in ln or (key in ln and "%s" in ln) or f'"{key}"' in ln:
                idx = i
                break
        assert idx is not None, f"{key} is never written by the script"
        idxs.append(idx)
    span = max(idxs) - min(idxs)
    assert span <= 12, (
        "the three env keys must be written as one adjacent block "
        f"(span={span} code lines)"
    )
    window = lines[max(0, min(idxs) - 8) : max(idxs) + 4]
    assert any(
        _ENV_FILE in ln or "ENV_FILE" in ln for ln in window
    ), (
        "the env keys must be written INTO .pipeline.env (redirect/tee/heredoc "
        "onto the env file)"
    )


def test_env_file_writes_agents_dir_absolute_no_tilde():
    """AGENTS_DIR is written into .pipeline.env, absolute, no tilde -
    mirrors PLAN_DIR/WORKTREE_ROOT's contract (CFG-D1)."""
    src = _source()
    line = _line_containing(src, "AGENTS_DIR=")
    assert line is not None, (
        "AGENTS_DIR must be written into .pipeline.env, alongside PLAN_DIR/"
        "WORKTREE_ROOT/PIPELINE_AUTONOMY, so the dashboard and scheduler "
        "processes launched by `up` resolve personas from the provisioned "
        "data dir rather than falling back to the operator's global "
        "~/.claude/agents/ default"
    )
    assert "~" not in line, (
        f"AGENTS_DIR must be written as an absolute path, without a tilde "
        f"(the sourced env file does no tilde expansion): {line!r}"
    )


def test_agents_dir_is_provisioned_before_both_processes_are_launched():
    """AGENTS_DIR is written into .pipeline.env before dashboard.sh/
    scheduler.sh run, same ordering contract as PLAN_DIR/WORKTREE_ROOT."""
    src = _source()
    agents_idx = _line_index(src, "AGENTS_DIR=")
    dash_idx = _line_index(src, _DASHBOARD_SH)
    sched_idx = _line_index(src, _SCHEDULER_SH)
    assert None not in (agents_idx, dash_idx, sched_idx), "wiring anchors missing"
    assert agents_idx < dash_idx, (
        "AGENTS_DIR must be written before scripts/dashboard.sh is launched"
    )
    assert agents_idx < sched_idx, (
        "AGENTS_DIR must be written before scripts/scheduler.sh is launched"
    )


# --------------------------------------------------------------------------- #
# PIPELINE_BACKEND_DISPATCH: `up` derives the dispatch backend from
# model_registry.json's roles.dispatch.provider and writes it into the SAME
# env-writing brace block -- unless the operator already exported their own
# PIPELINE_BACKEND_DISPATCH, which always wins.  Source-text probes only, like
# everything above: the runtime check (registry provider "ollama", env unset,
# `up` -> .pipeline.env contains PIPELINE_BACKEND_DISPATCH="ollama") is
# verified by hand per the story brief; this suite pins the source contract
# that produces it.  AGENTSPROV-1's AGENTS_DIR provisioning is a sibling
# story's work and has its own probes above -- these tests never pin the env
# block's total contents, only membership/ordering against fixed anchors.
# --------------------------------------------------------------------------- #
_DISPATCH_WRITE_RE = re.compile(r'PIPELINE_BACKEND_DISPATCH=\\?"[$%]')


def _dispatch_write_index(src):
    """Index (into code lines) of the line WRITING PIPELINE_BACKEND_DISPATCH
    into the env file, else None.

    The written value must be quoted and variable-derived (the block's own
    comment explains why every value is quoted), so the primary probe demands
    a (possibly backslash-escaped) double quote followed by a `$` expansion --
    which also rules out a hardcoded provider name; a printf '%s' form matches
    via the `%` arm of the character class.
    """
    lines = _code_lines(src)
    candidates = [
        i
        for i, ln in enumerate(lines)
        if "PIPELINE_BACKEND_DISPATCH=" in ln and "==>" not in ln
    ]
    for i in candidates:
        if _DISPATCH_WRITE_RE.search(lines[i]):
            return i
    for i in candidates:
        if "$" in lines[i]:
            return i
    return None


def test_env_file_writes_pipeline_backend_dispatch_inside_env_block():
    """PIPELINE_BACKEND_DISPATCH is written into .pipeline.env inside the same
    brace block: after the PIPELINE_AUTONOMY line, before } >"$ENV_FILE."""
    src = _source()
    autonomy_idx = _line_index(src, "PIPELINE_AUTONOMY=")
    closer_idx = _line_index(src, '>"$ENV_FILE"')
    write_idx = _dispatch_write_index(src)
    assert autonomy_idx is not None, "the PIPELINE_AUTONOMY write line is missing"
    assert closer_idx is not None, 'the } >"$ENV_FILE" block closer is missing'
    assert write_idx is not None, (
        "cmd_up() never writes PIPELINE_BACKEND_DISPATCH into .pipeline.env; "
        "echo it into the env-writing brace block, quoted and derived from the "
        "registry (see the sibling PLAN_DIR/WORKTREE_ROOT/PIPELINE_AUTONOMY/"
        "AGENTS_DIR echoes for the exact style)"
    )
    assert autonomy_idx < write_idx, (
        "PIPELINE_BACKEND_DISPATCH must be written after the PIPELINE_AUTONOMY "
        "line in the env block"
    )
    assert write_idx < closer_idx, (
        "PIPELINE_BACKEND_DISPATCH must be echoed INSIDE the brace block so it "
        f'lands in the same >"$ENV_FILE" redirect (write at code line '
        f"{write_idx}, closer at {closer_idx})"
    )
    line = _code_lines(src)[write_idx]
    assert _DISPATCH_WRITE_RE.search(line), (
        "the written value must be quoted and variable-derived, never a "
        f"hardcoded provider name (mirror echo \"KEY=\\\"$VAR\\\"\")): {line!r}"
    )


def test_dispatch_value_is_derived_from_model_registry_roles_dispatch():
    """The written value is READ from model_registry.json's
    .roles.dispatch.provider, never hardcoded."""
    src = _source()
    lines = _code_lines(src)
    read_idx = next(
        (
            i
            for i, ln in enumerate(lines)
            if "roles.dispatch" in ln and "==>" not in ln
        ),
        None,
    )
    if read_idx is None:
        # A grep/sed fallback has no dotted jq path; it targets the same
        # "dispatch"/"provider" keys inside model_registry.json instead.
        read_idx = next(
            (
                i
                for i, ln in enumerate(lines)
                if "model_registry" in ln
                and '"dispatch"' in ln
                and "provider" in ln
                and "==>" not in ln
            ),
            None,
        )
    assert read_idx is not None, (
        "the dispatch value must be read from model_registry.json's "
        "roles.dispatch.provider (jq path `.roles.dispatch.provider`, or a "
        'grep/sed over the same "dispatch"/"provider" keys) -- a hardcoded '
        "backend name would desync the script from the registry"
    )
    window = "\n".join(lines[max(0, read_idx - 4) : read_idx + 5])
    assert "model_registry" in window, (
        "the roles.dispatch lookup must target model_registry.json (no "
        f"model_registry reference near code line {read_idx})"
    )
    if "jq" in window:
        assert re.search(r"\bgrep\b|\bsed\b|VENV_PY|python", window), (
            "jq is not guaranteed to be installed (verify_health deliberately "
            "parses JSON with the venv python, 'no jq dependency'): keep a "
            "fallback (grep/sed one-liner or the existing $VENV_PY -c style) "
            "next to the jq read"
        )
    assert re.search(
        r"2>/dev/null|2>&1|\|\| true|\|\| :|\|\| echo|// empty"
        r"|\[\s+-[fr]\s|command -v|if\s+\[\s+-[fr]",
        window,
    ), (
        "the registry read must fail open: suppress jq/grep errors "
        "(2>/dev/null, || true, jq's // empty) or pre-test the file "
        "(-f/-r/command -v) so a missing, unreadable or malformed "
        "model_registry.json writes nothing extra instead of killing `up`"
    )
    write_idx = _dispatch_write_index(src)
    assert write_idx is not None, "no PIPELINE_BACKEND_DISPATCH write line found"
    lo, hi = min(read_idx, write_idx), max(read_idx, write_idx)
    guarded = "\n".join(lines[lo : hi + 1])
    assert re.search(r"-[nz]\s+\"\$\{?[A-Za-z_]", guarded), (
        "an empty provider (registry present but roles.dispatch.provider "
        "absent/null) must write NOTHING extra: guard the write with a -n/-z "
        "test on the resolved provider value, matching the script's existing "
        "[ -n ... ] style"
    )


def test_operator_env_value_wins_over_registry_default():
    """NEGATIVE/boundary: an operator-exported PIPELINE_BACKEND_DISPATCH is
    never silently overridden -- the write is conditional on a set-u-safe
    emptiness test of the operator's own value, placed before the write."""
    src = _source()
    lines = _code_lines(src)
    guard_idx = None
    for i, ln in enumerate(lines):
        if re.search(r"\$\{PIPELINE_BACKEND_DISPATCH:-", ln) or re.search(
            r'\[\s+-[nz]\s+"\$PIPELINE_BACKEND_DISPATCH\s*"', ln
        ):
            guard_idx = i
            break
    assert guard_idx is not None, (
        "the write must be conditional on the operator's own environment: test "
        "${PIPELINE_BACKEND_DISPATCH:-} (the script runs under `set -euo "
        "pipefail`, so a bare $PIPELINE_BACKEND_DISPATCH would abort under "
        "set -u when the variable is unset) before writing the "
        "registry-derived value"
    )
    write_idx = _dispatch_write_index(src)
    assert write_idx is not None, "no PIPELINE_BACKEND_DISPATCH write line found"
    assert guard_idx <= write_idx, (
        "the operator-env guard must run BEFORE (or on the same line as, via "
        f"||) the registry-derived write (guard at code line {guard_idx}, "
        f"write at {write_idx}) so an explicit operator choice always wins "
        "over the convenience default"
    )


def test_dispatch_derivation_never_kills_up_on_registry_problems():
    """NEGATIVE: a missing, unreadable or provider-less model_registry.json
    must silently write nothing extra -- no die/exit anywhere near the
    derivation (the default-to-claude fallback in pipeline/dispatch.py is
    legitimate for a genuinely unconfigured install and must not become an
    error here)."""
    src = _source()
    lines = _code_lines(src)
    anchors = [i for i, ln in enumerate(lines) if "model_registry" in ln]
    write_idx = _dispatch_write_index(src)
    if write_idx is not None:
        anchors.append(write_idx)
    assert anchors, (
        "no dispatch-derivation anchors found (model_registry reference / "
        "write line) -- the derivation this story adds is missing entirely"
    )
    for i in anchors:
        window = "\n".join(lines[max(0, i - 2) : i + 3])
        assert not re.search(r"\b(die|exit)\b", window), (
            "the dispatch derivation must fail open: no die/exit may sit near "
            f"the registry read or the write (code line {i}):\n{window}"
        )


def test_dispatch_write_happens_before_both_processes_are_launched():
    """Same ordering contract as PLAN_DIR/WORKTREE_ROOT/AGENTS_DIR: the
    registry-derived value is in the env file before dashboard.sh and
    scheduler.sh source it."""
    src = _source()
    write_idx = _dispatch_write_index(src)
    dash_idx = _line_index(src, _DASHBOARD_SH)
    sched_idx = _line_index(src, _SCHEDULER_SH)
    assert None not in (write_idx, dash_idx, sched_idx), "wiring anchors missing"
    assert write_idx < dash_idx, (
        "PIPELINE_BACKEND_DISPATCH must be written before scripts/dashboard.sh "
        "is launched"
    )
    assert write_idx < sched_idx, (
        "PIPELINE_BACKEND_DISPATCH must be written before scripts/scheduler.sh "
        "is launched"
    )


def test_up_prints_which_dispatch_outcome_happened():
    """One `==> ` status line says which of the three outcomes happened:
    set from the registry / left at the operator's existing env value / left
    unset because the registry had no roles.dispatch.provider."""
    src = _source()
    lines = _code_lines(src)
    status_idx = [
        i
        for i, ln in enumerate(lines)
        if "==>" in ln and "PIPELINE_BACKEND_DISPATCH" in ln
    ]
    assert status_idx, (
        'print one `echo "==> ..."` status line naming PIPELINE_BACKEND_DISPATCH '
        "so a user watching `up` run can see which outcome happened (mirrors "
        'the existing `echo "==> wrote $ENV_FILE"` verbosity)'
    )
    context = "\n".join(
        "\n".join(lines[max(0, i - 3) : i + 4]) for i in status_idx
    )
    assert re.search(r"(?i)registr", context), (
        "the status line must distinguish 'set from model_registry.json' "
        "(name the registry)"
    )
    assert re.search(r"(?i)operator|existing|already", context), (
        "the status line must distinguish 'left at the operator's existing "
        "env value'"
    )
    assert re.search(r"(?i)unset|none|not set", context), (
        "the status line must distinguish 'left unset because the registry "
        "had no roles.dispatch.provider'"
    )


def test_dispatch_py_env_first_resolution_stays_intact():
    """Tripwire (out-of-scope guard): this story only makes standalone-setup.sh
    populate the env var on a fresh install; the env-first dispatch resolution
    in pipeline/dispatch.py is deliberate and must not be touched."""
    dispatch_py = _REPO_ROOT / "pipeline" / "dispatch.py"
    assert dispatch_py.exists(), "pipeline/dispatch.py is missing"
    assert "PIPELINE_BACKEND_DISPATCH" in dispatch_py.read_text(encoding="utf-8"), (
        "pipeline/dispatch.py no longer references PIPELINE_BACKEND_DISPATCH; "
        "if the env-first resolution moved elsewhere, re-point this tripwire -- "
        "but do NOT change the resolution priority as part of the "
        "standalone-setup story"
    )


def test_env_values_are_absolute_not_tilde():
    """Env values are ABSOLUTE: no tilde in the written lines, and the data
    dir is absolutised (cd/pwd, readlink -f or realpath)."""
    src = _source()
    for ln in _code_lines(src):
        for key in ("PLAN_DIR", "WORKTREE_ROOT"):
            if f"{key}=" in ln:
                assert "~" not in ln, (
                    f"{key} must be written as an absolute path, without a "
                    f"tilde (the sourced env file does no tilde expansion): "
                    f"{ln!r}"
                )
    data_dir_line = _line_containing(src, "pipeline-standalone")
    assert data_dir_line is not None, (
        "the default --data-dir must be ~/pipeline-standalone"
    )
    home_anchored = "HOME" in data_dir_line or re.search(
        r"(?<![\w\"])~/pipeline-standalone", data_dir_line
    )
    assert home_anchored, (
        f"the data dir default must be HOME-anchored, got {data_dir_line!r}"
    )
    var = _data_dir_var(src)
    assert var is not None, "could not locate the data-dir variable"
    absolutised = (
        _line_containing(src, var, "pwd")
        or _line_containing(src, var, "readlink")
        or _line_containing(src, var, "realpath")
    )
    assert absolutised is not None, (
        f"{var} must be absolutised (cd ... && pwd, readlink -f or realpath) "
        "before PLAN_DIR/WORKTREE_ROOT are derived from it"
    )


def test_existing_env_file_is_backed_up_unless_force():
    """An existing .pipeline.env is backed up (guarded by an existence test),
    and --force is parsed before that backup decision."""
    src = _source()
    lines = _code_lines(src)
    backup_idx, backup_line = _find(
        src,
        r"(bak|backup|\.save)",
    )
    assert backup_line is not None and (
        _ENV_FILE in backup_line or "ENV_FILE" in backup_line
    ), (
        "an existing .pipeline.env must be backed up (e.g. .pipeline.env.bak) "
        "rather than clobbered"
    )
    force_idx = _line_index(src, "--force")
    assert force_idx is not None, "--force must be parsed"
    assert force_idx < backup_idx, (
        "--force must be handled before the backup decision is made"
    )
    guard_context = lines[max(0, backup_idx - 5) : backup_idx]
    assert any(
        re.search(r"-f\s|-e\s|-s\s|\[\[", ln) for ln in guard_context
    ), "the backup must be guarded by an existence test on the env file"
    window = lines[max(0, backup_idx - 6) : backup_idx + 6]
    assert any("force" in ln.lower() for ln in window), (
        "the backup must be skippable with --force"
    )


def test_autonomy_defaults_to_dry_run():
    """PIPELINE_AUTONOMY defaults to dry-run."""
    src = _source()
    assert _line_containing(src, "PIPELINE_AUTONOMY") is not None, (
        "PIPELINE_AUTONOMY must be written"
    )
    code = _code(src)
    markers = (':-dry-run', ':-"dry-run"', ":-'dry-run'", '="dry-run"', "=dry-run")
    assert any(marker in code for marker in markers), (
        "PIPELINE_AUTONOMY must default to dry-run "
        "(e.g. AUTONOMY=\"${AUTONOMY:-dry-run}\")"
    )


# --------------------------------------------------------------------------- #
# (e) launch + the verify step that gates success
# --------------------------------------------------------------------------- #
def test_up_launches_dashboard_and_scheduler():
    """Both helpers are invoked (not merely mentioned in comments)."""
    src = _source()
    dash = _line_containing(src, _DASHBOARD_SH)
    sched = _line_containing(src, _SCHEDULER_SH)
    assert dash is not None and sched is not None
    for line, name in ((dash, "dashboard"), (sched, "scheduler")):
        assert "$" in line or "bash" in line or re.search(r"\bsh\b", line), (
            f"{name} must be invoked, not merely mentioned: {line!r}"
        )


def test_health_poll_hits_api_health_with_retries():
    """GET /api/health is polled (retry loop + sleep) until it answers."""
    src = _source()
    idx = _line_index(src, "/api/health")
    assert idx is not None, "`up` must verify GET /api/health before succeeding"
    window = _code_lines(src)[max(0, idx - 6) : idx + 12]
    text = "\n".join(window)
    assert re.search(r"\buntil\b|\bwhile\b|\bfor\b|--retry", text), (
        "the health check must poll/retry until the server answers"
    )
    assert "sleep" in text, "the poll must sleep between attempts"


def test_health_url_uses_the_configured_port():
    """The health URL is built from the configured --port (default 8001)."""
    line = _line_containing(_source(), "http", "PORT")
    assert line is not None, (
        "the /api/health URL must be built from the configured port variable"
    )


def test_health_request_sends_the_api_key_header():
    """The health request carries x-pipeline-api-key, keyed via
    app.auth.get_or_create_api_key."""
    src = _source()
    assert _line_containing(src, "x-pipeline-api-key") is not None, (
        "the health request must send the x-pipeline-api-key header"
    )
    assert _line_containing(src, "get_or_create_api_key") is not None, (
        "the API key must be obtained from app.auth.get_or_create_api_key"
    )
    assert _line_containing(src, "app.auth") is not None, (
        "the key lookup must go through the app.auth module"
    )
    health_idx = _line_index(src, "/api/health")
    key_idx = _line_index(src, "x-pipeline-api-key")
    assert health_idx is not None and key_idx is not None
    assert abs(health_idx - key_idx) <= 8, (
        "the x-pipeline-api-key header must be attached to the /api/health "
        "request itself"
    )


def test_health_verification_asserts_plan_dir_matches_intended_path():
    """The health payload's plan_dir is asserted against the intended path."""
    src = _source()
    idx = _line_index(src, "plan_dir")
    assert idx is not None, (
        "the /api/health plan_dir field must be asserted against the intended "
        "PLAN_DIR"
    )
    window = _code_lines(src)[max(0, idx - 6) : idx + 8]
    assert any(
        ("PLAN_DIR" in ln or "DATA_DIR" in ln or "plans" in ln) for ln in window
    ), "plan_dir must be compared with the intended path"


def test_config_mismatch_branch_exits_non_zero_and_names_fields():
    """A non-empty config_mismatch exits non-zero with a message naming the
    diverging fields."""
    src = _source()
    region = _mismatch_region(src)
    assert region, "config_mismatch from /api/health must be checked"
    text = "\n".join(region)
    assert re.search(r"\b(?:exit|return)\s+[1-9]\b|\bdie\b", text), (
        "a non-empty config_mismatch must exit non-zero"
    )
    assert any(
        re.search(r"\b(?:echo|printf|cat|log|die|fail|err)\b.*\$", ln)
        for ln in region
    ), (
        "the mismatch message must name the diverging fields (interpolate the "
        "config_mismatch value into the error output)"
    )


def test_success_banner_is_not_printed_on_mismatch():
    """No success banner inside the config_mismatch branch; the happy path
    does print one."""
    src = _source()
    region = _mismatch_region(src)
    assert region, "config_mismatch guard missing"
    banners = [ln for ln in region if _is_banner_line(ln)]
    assert not banners, (
        "a success banner must NOT be printed when config_mismatch is "
        f"non-empty: {banners!r}"
    )
    assert any(
        _is_banner_line(ln) for ln in _code_lines(src)
    ), "the happy path must print a success banner (e.g. '==> standalone up')"


# --------------------------------------------------------------------------- #
# (f) defaults
# --------------------------------------------------------------------------- #
def test_default_port_is_8001_not_8000():
    """Default --port is 8001 and nothing defaults to 8000."""
    src = _source()
    port_line = _line_containing(src, "8001")
    assert port_line is not None, "the default port must be 8001"
    assert "PORT" in port_line or re.search(r"(?i)\bport\b", port_line), (
        f"8001 must be the port default, got {port_line!r}"
    )
    code = _code(src)
    for banned in (":-8000", "=8000", '"8000"', "'8000'"):
        assert banned not in code, (
            f"the port must not default to 8000 (found {banned!r}); an "
            "operator's existing dashboard may already own 8000"
        )


def test_default_data_dir_is_pipeline_standalone_under_home():
    """Default --data-dir is ~/pipeline-standalone."""
    line = _line_containing(_source(), "pipeline-standalone")
    assert line is not None, "the default --data-dir must be ~/pipeline-standalone"


# --------------------------------------------------------------------------- #
# (g) down / status
# --------------------------------------------------------------------------- #
def test_down_stops_both_processes_and_keeps_scratch_data():
    """`down` stops BOTH processes and says the scratch data stays put."""
    src = _source()
    region = _subcommand_region(src, "down")
    assert region, "down subcommand missing"
    text = "\n".join(region)
    assert re.search(r"\b(?:kill|pkill|stop)\b", text), (
        "down must stop the launched processes"
    )
    low = text.lower()
    assert "dashboard" in low and "scheduler" in low, (
        "down must stop BOTH the dashboard and the scheduler"
    )
    kept = _find(
        src,
        r"kept|left in place|preserv|remain|untouch|not delet|won.t be delet",
    )
    assert kept[1] is not None, (
        "down must say that the scratch data is left in place (not deleted)"
    )
    for ln in region:
        if re.search(r"\brm\s+-r?f?\w*\b", ln):
            assert not re.search(
                r"DATA_DIR|pipeline-standalone|/plans|/worktrees|/repo\b", ln
            ), f"down must not delete the scratch data: {ln!r}"


def test_status_reports_paths_and_process_state():
    """`status` prints the resolved paths and both processes' state."""
    src = _source()
    region = _subcommand_region(src, "status")
    assert region, "status subcommand missing"
    text = "\n".join(region)
    assert re.search(
        r"PLAN_DIR|WORKTREE_ROOT|DATA_DIR|data-dir|pipeline-standalone", text
    ), "status must print the resolved paths"
    assert re.search(
        r"running|stopped|pgrep|ps\s|lsof|kill -0|curl",
        text,
        re.IGNORECASE,
    ), "status must print both processes' state"


def test_no_destructive_removal_of_user_data():
    """NEGATIVE: nothing in the script rm's the data dir or its contents."""
    for ln in _code_lines(_source()):
        if re.search(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\b", ln):
            assert not re.search(
                r"DATA_DIR|pipeline-standalone|/plans|/worktrees|/repo\b", ln
            ), f"the script must never delete user data: {ln!r}"


# --------------------------------------------------------------------------- #
# (review regressions, REQUEST_CHANGES): the API-key lookup must not resolve
# `app` via the caller's cwd, and `down` must never report success when it
# found no pid file to signal.  Unlike the probes above, the `down` test
# EXECUTES the script -- but only against an empty temp data dir: no
# processes are started, nothing is signalled, no repo state is touched.
# --------------------------------------------------------------------------- #
def test_api_key_lookup_imports_app_from_repo_root_not_caller_cwd():
    """verify_health()'s API-key lookup must force cwd to the repo root.

    The venv does NOT install the `app` package (scripts/install.sh only
    pip-installs requirements*.txt; pyproject's pythonpath=["."] is
    pytest-only), so a bare `"$VENV_PY" -c 'from app.auth import ...'`
    resolves `app` from the CALLER's cwd.  From any cwd other than the repo
    root, `up` then dies with ModuleNotFoundError AFTER both processes are
    already running -- or, when the ambient cwd holds a different checkout's
    app/, silently reads that repo's key and misdiagnoses "dashboard did not
    become healthy" after 60s of 401 polls.  The lookup must be wrapped as
    `( cd "$REPO_ROOT" && "$VENV_PY" -c ... )`; a PYTHONPATH prefix alone is
    NOT sufficient, because for `python -c` the cwd (sys.path[0]) precedes
    PYTHONPATH, so a foreign checkout's app/ would still shadow $REPO_ROOT.
    """
    src = _source()
    idx = _line_index(src, "from app.auth import")
    assert idx is not None, (
        "the API-key lookup (`from app.auth import get_or_create_api_key`) "
        "is gone from the script; re-point this probe at wherever the key "
        "is now looked up"
    )
    lines = _code_lines(src)
    # The cd-guard may sit on the lookup's own line or a few lines above it
    # (multi-line subshell), so probe a small window around the import.
    window = "\n".join(lines[max(0, idx - 4) : idx + 2])
    same_line = re.search(
        r'cd\s+"\$REPO_ROOT"\s*\\?\s*(?:\d?>\s*\S+\s*)?&&\s*"?\$\{?VENV_PY\}?',
        window,
    )
    guarded_next_line = re.search(
        r'cd\s+"\$REPO_ROOT"[^\n]*\|\|[^\n]*\n\s*"?\$\{?VENV_PY\}?', window
    )
    assert same_line or guarded_next_line, (
        "the $VENV_PY invocation importing `app` (the API-key lookup in "
        "verify_health) is cwd-dependent: it resolves `app` via the caller's "
        "cwd, but `app` is NOT installed in the venv.  Wrap the lookup as "
        '`( cd "$REPO_ROOT" && "$VENV_PY" -c ... )` -- a PYTHONPATH prefix '
        "alone is not sufficient, because for `python -c` the cwd "
        "(sys.path[0]) precedes PYTHONPATH.  Offending line: "
        f"{lines[idx]!r}"
    )


def test_down_against_unprovisioned_data_dir_exits_nonzero():
    """`down` must not report success when it found no pid file to signal.

    `down`/`status` only tilde-expand DATA_DIR and never absolutise it
    (unlike `up`'s `cd ... && pwd`), so a relative --data-dir combined with a
    changed cwd resolves to a directory that was never provisioned -- and
    `down` used to print its success banner and exit 0 anyway.
    """
    with tempfile.TemporaryDirectory(prefix="standalone-down-") as tmp:
        data_dir = Path(tmp) / "never-provisioned"
        data_dir.mkdir()
        proc = subprocess.run(
            ["bash", str(_SCRIPT), "down", "--data-dir", str(data_dir)],
            cwd=tmp,  # deliberately NOT the repo root
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode != 0, (
            "`down` against a data dir with no pid files exited 0: it "
            "reported success while signalling nothing.  `down` must resolve "
            "DATA_DIR exactly like `up` (absolutise via `cd ... && pwd`, "
            "dying if it does not resolve) and must exit non-zero when it "
            "finds no pid file to signal.\n"
            f"--- stdout ---\n{proc.stdout}--- stderr ---\n{proc.stderr}"
        )
        assert data_dir.is_dir(), (
            "`down` must leave the scratch data in place (no destructive "
            "rm), even when it finds nothing to stop"
        )
