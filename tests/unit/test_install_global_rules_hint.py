"""Static wiring tests for the opt-in global-rules hint in scripts/install.sh.

The installer prints a *guarded* opt-in pointer at
``scripts/install_global_rules.py`` when claude/codex/opencode is on PATH; it
must never run that script and must never prompt.  install.sh is a SHARED
artifact later stories may extend, so these tests assert MEMBERSHIP and
ORDERING against fixed anchors only -- never total contents, line count or a
hash.  RED state: the hint literals are absent until the implementation lands.
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INSTALL_SH = _REPO_ROOT / "scripts" / "install.sh"
_REMOTE_INSTALL_SH = _REPO_ROOT / "scripts" / "remote-install.sh"

_MODULE_LITERAL = "install_global_rules.py"
_EXAMPLE_CMD = "scripts/install_global_rules.py --tools=claude"

# Distinctive fragments of the required hint prose (membership, not wording).
_HINT_FRAGMENTS = (
    "Optional:",
    "engineering + pipeline rules",
    "opt-in",
    "nothing is written until you run it",
)

# The three CLIs whose presence gates the hint.
_GATE_TOOLS = ("claude", "codex", "opencode")

# A line that would *execute* the opt-in script rather than echo it.
_EXECUTION_RE = re.compile(
    r"(?m)^\s*(?:[.\w/]*python[0-9.]*|bash|sh|zsh|source|exec|\.)\b.*"
    + re.escape(_MODULE_LITERAL)
)

# A line that would block on user input.
_PROMPT_RE = re.compile(r"(?m)^\s*read\s+(-[a-zA-Z]+\s+)*\S")


def _source():
    return _INSTALL_SH.read_text(encoding="utf-8")


def _lines(source):
    return source.splitlines()


def _flat(source):
    """Source with whitespace runs collapsed, so wrapped echoes still match."""
    return re.sub(r"\s+", " ", source)


def _index_of(source, *needles):
    """Index of the first line containing all needles, else None."""
    for i, ln in enumerate(_lines(source)):
        if all(needle in ln for needle in needles):
            return i
    return None


def _mention_lines(source):
    """Every line that mentions the opt-in script."""
    return [ln for ln in _lines(source) if _MODULE_LITERAL in ln]


def _hint_index(source):
    """Index of the first line carrying any distinctive hint fragment."""
    for i, ln in enumerate(_lines(source)):
        if any(frag in ln for frag in _HINT_FRAGMENTS):
            return i
    return None


def _is_echo(line):
    return line.lstrip().startswith("echo ")


def _is_comment(line):
    return line.lstrip().startswith("#")


# --- (a) the hint text and its example command are present ------------------ #
def test_hint_text_is_present():
    """(a) The opt-in hint prose appears in install.sh."""
    flat = _flat(_source())
    missing = [frag for frag in _HINT_FRAGMENTS if frag not in flat]
    assert not missing, f"hint prose fragments missing from install.sh: {missing!r}"


def test_hint_is_printed_by_an_echo_line():
    """The hint is *printed* (echo), not a bare comment."""
    echoed = " ".join(ln for ln in _lines(_source()) if _is_echo(ln))
    missing = [frag for frag in _HINT_FRAGMENTS if frag not in echoed]
    assert not missing, f"no echo line prints the opt-in hint; missing {missing!r}"


def test_example_command_is_present_with_dry_run():
    """(a) The example command names the script, --tools=claude and --dry-run."""
    flat = _flat(_source())
    assert _EXAMPLE_CMD in flat, "example command missing from install.sh"
    assert "--dry-run" in flat, "example command must show the --dry-run flag"


def test_example_command_is_echoed_not_run():
    """The example command lives on an echo line (it is documentation)."""
    mentions = _mention_lines(_source())
    assert mentions, "install.sh never mentions the opt-in script"
    assert any(_is_echo(ln) and "--tools=claude" in ln for ln in mentions), (
        f"example command must be echoed, not executed: {mentions!r}"
    )


# --- (b) the hint is guarded by a `command -v` test ------------------------- #
def _guard_line(source):
    for ln in _lines(source):
        stripped = ln.strip()
        if stripped.startswith("if command -v") and "then" in stripped:
            return ln
    return None


def test_hint_is_guarded_by_command_v_test():
    """(b) An `if command -v ...; then` guard exists for the three CLIs."""
    guard = _guard_line(_source())
    assert guard is not None, "no `if command -v ...; then` guard found in install.sh"
    for tool in _GATE_TOOLS:
        assert f"command -v {tool} >/dev/null 2>&1" in guard, (
            f"guard must test for {tool!r}: {guard!r}"
        )
    assert "||" in guard, f"guard must OR the three lookups: {guard!r}"


def test_hint_is_inside_the_guard_block():
    """(b) The hint echo sits between the guard's `if` and its closing `fi`."""
    source = _source()
    lines = _lines(source)
    guard_i = _index_of(source, "if command -v", "then")
    hint_i = _hint_index(source)
    assert guard_i is not None, "guard line missing"
    assert hint_i is not None, "hint line missing"
    assert guard_i < hint_i, "hint must come after the guard opens"
    fi_i = next(
        (i for i in range(guard_i + 1, len(lines)) if lines[i].strip() == "fi"),
        None,
    )
    assert fi_i is not None, "guard block is not closed by `fi`"
    assert hint_i < fi_i, "the hint must be inside the guard block, before its `fi`"


def test_hint_is_ordered_after_next_steps():
    """(b) The hint is ordered after the existing 'Next steps' line."""
    source = _source()
    next_steps_i = _index_of(source, "Next steps")
    hint_i = _hint_index(source)
    assert next_steps_i is not None, "'Next steps' anchor missing from install.sh"
    assert hint_i is not None, "hint line missing"
    assert hint_i > next_steps_i, (
        "the opt-in hint must be printed after the 'Next steps' header"
    )


# --- (c) negative / security: never execute, never prompt ------------------- #
def test_every_mention_of_the_script_is_echo_or_comment():
    """(c) No line mentioning the opt-in script is a command line."""
    mentions = _mention_lines(_source())
    assert mentions, "install.sh never mentions the opt-in script"
    offenders = [ln for ln in mentions if not (_is_echo(ln) or _is_comment(ln))]
    assert not offenders, (
        f"install.sh must only echo the opt-in script, never invoke it: {offenders!r}"
    )


def test_installer_never_executes_the_opt_in_script():
    """(c) No python/bash/source invocation of the opt-in script survives."""
    match = _EXECUTION_RE.search(_source())
    assert match is None, (
        f"install.sh must not execute the opt-in script: {match.group(0)!r}"
    )


def test_installer_never_prompts():
    """(c) The installer must not block on user input."""
    match = _PROMPT_RE.search(_source())
    assert match is None, f"install.sh must not prompt: {match.group(0)!r}"


def test_remote_install_sh_is_untouched():
    """The hint belongs in install.sh only; remote-install.sh stays as-is."""
    if not _REMOTE_INSTALL_SH.exists():
        return
    remote = _REMOTE_INSTALL_SH.read_text(encoding="utf-8")
    assert "Optional:" not in remote, (
        "the opt-in hint must not be added to remote-install.sh"
    )
    assert "engineering + pipeline rules" not in remote, (
        "the opt-in hint must not be added to remote-install.sh"
    )
