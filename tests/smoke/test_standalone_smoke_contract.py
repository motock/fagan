"""Contract tests for the gated standalone smoke (SA-18).

The standalone smoke proves the operator path end to end in CI: `deploy/install.sh
--non-interactive` brings up deploy/standalone/compose.yaml, then
deploy/standalone/smoke.sh drives a `repo_url` plan through the MCP transport.

These tests guard the two things that silently break such a job: a workflow that
is not actually gated (so it runs, or never runs, when it should do the other),
and an `uses:` pinned to a mutable tag instead of a commit SHA. PyYAML is not a
dependency of this repo, so the files are read as text.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "standalone-smoke.yml"
SCRIPT = REPO_ROOT / "deploy" / "standalone" / "smoke.sh"

GATE = re.compile(r"ENABLE_STANDALONE_SMOKE_CI\s*==\s*'true'")
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)", re.MULTILINE)
SHA = re.compile(r"^[0-9a-f]{40}$")


def _uses_refs(text: str) -> list[str]:
    """The git ref of every `uses:` line: the text after the last '@'."""
    refs = []
    for action in USES.findall(text):
        ref = action.rsplit("@", 1)[-1]
        refs.append(ref.split("#", 1)[0].strip())
    return refs


def _is_sha_ref(ref: str) -> bool:
    return bool(SHA.match(ref))


def test_workflow_exists():
    assert WORKFLOW.exists(), f"{WORKFLOW} missing"


def test_job_is_gated_on_the_repository_variable():
    assert WORKFLOW.exists(), f"{WORKFLOW} missing"
    text = WORKFLOW.read_text(encoding="utf-8")
    assert GATE.search(text), (
        "the standalone smoke job must be gated with "
        "`if: vars.ENABLE_STANDALONE_SMOKE_CI == 'true'`; a bare "
        "`ENABLE_STANDALONE_SMOKE_CI` is truthy for the string \"false\""
    )


def test_every_action_is_pinned_to_a_full_commit_sha():
    assert WORKFLOW.exists(), f"{WORKFLOW} missing"
    refs = _uses_refs(WORKFLOW.read_text(encoding="utf-8"))
    assert refs, "no `uses:` line found in the workflow"
    unpinned = [ref for ref in refs if not _is_sha_ref(ref)]
    assert not unpinned, f"`uses:` refs that are not 40-hex commit SHAs: {unpinned}"


def test_script_parses():
    assert SCRIPT.exists(), f"{SCRIPT} missing"
    proc = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"bash -n rejected {SCRIPT}:\n{proc.stderr}"


def test_script_registers_a_teardown_trap():
    assert SCRIPT.exists(), f"{SCRIPT} missing"
    text = SCRIPT.read_text(encoding="utf-8")
    assert "trap" in text, (
        "smoke.sh must register `trap cleanup EXIT` before the stack starts, so a "
        "failure mid-run still runs `compose down -v` and the next run is clean"
    )


def test_sha_validator_covers_the_negative_case():
    # The pinning check above is only meaningful if it actually rejects a tag.
    assert not _is_sha_ref("v4")
    assert _is_sha_ref("0123456789abcdef0123456789abcdef01234567")


def test_gate_regex_covers_the_negative_case():
    # A gate without the explicit comparison would run when the variable is "false".
    assert not GATE.search("    if: vars.ENABLE_STANDALONE_SMOKE_CI")


# ---------------------------------------------------------------------------
# SA-G2: the escalated installer has to keep the compose shim, macOS must run
# unattended without sudo, and a failed run must be inspectable.
# ---------------------------------------------------------------------------

# `if [ "$(id -u)" -ne 0 ] && [ "$(uname -s)" != "Darwin" ]`, tolerant of spacing.
SUDO_GUARD = re.compile(
    r'\[\s*"\$\(id -u\)"\s+-ne\s+0\s*\]'
    r'\s*&&\s*'
    r'\[\s*"\$\(uname -s\)"\s*!=\s*"Darwin"\s*\]'
)
KEEP_DEFAULT = re.compile(r"\$\{STANDALONE_SMOKE_KEEP:-0\}")
INSPECT_COMMAND = (
    r'Inspect with: docker compose -f \"$BASE_COMPOSE\" -f \"$OVERRIDE\" ps'
)


def _smoke_text() -> str:
    assert SCRIPT.exists(), f"{SCRIPT} missing"
    return SCRIPT.read_text(encoding="utf-8")


def test_smoke_script_parses_after_the_shim_change():
    proc = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"bash -n rejected {SCRIPT}:\n{proc.stderr}"


def test_smoke_script_parses_without_the_keep_flag():
    # Negative case: STANDALONE_SMOKE_KEEP unset in the child environment, so a
    # guard that reads it unguarded would be an unbound variable. `bash -n` cannot
    # catch that (it is a run-time error under `set -u`), which is why the `:-`
    # default is asserted separately below.
    proc = subprocess.run(
        ["env", "-u", "STANDALONE_SMOKE_KEEP", "bash", "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"bash -n rejected {SCRIPT} with STANDALONE_SMOKE_KEEP unset:\n{proc.stderr}"
    )


def test_stack_env_path_write_lands_after_install_shim():
    lines = _smoke_text().splitlines()
    shim = [i for i, line in enumerate(lines, 1) if "install_shim" in line]
    assert shim, "smoke.sh no longer references install_shim"
    # The PATH the escalated shell sources from the installer env file has to
    # contain $TMP/bin, so the write must happen after `install_shim` exports it.
    # One line, and it writes the env file: `printf 'PATH=%s\n' "$PATH" >>"$ENV_FILE"`.
    writes = [
        i
        for i, line in enumerate(lines, 1)
        if "PATH=%s" in line and ("ENV_FILE" in line or "stack.env" in line)
    ]
    assert len(writes) == 1, (
        "expected exactly one PATH= write aimed at the installer env file "
        f"(a single `printf 'PATH=%s'` line naming $ENV_FILE); found {writes}"
    )
    assert max(shim) < writes[0], (
        f"the stack.env PATH write (line {writes[0]}) must come after the last "
        f"install_shim (line {max(shim)}); otherwise the escalated `sudo bash -c` "
        "sources a PATH without $TMP/bin, install.sh reaches the real docker and "
        "the compose override is never applied"
    )


def test_sudo_is_skipped_on_darwin():
    text = _smoke_text()
    assert SUDO_GUARD.search(text), (
        "the sudo escalation must exclude macOS: "
        '`if [ "$(id -u)" -ne 0 ] && [ "$(uname -s)" != "Darwin" ]`; Docker Desktop '
        "maps bind mounts to the container uid, so sudo is not needed there"
    )
    guard = SUDO_GUARD.search(text)
    sudo = text.find('SUDO="sudo"')
    assert sudo != -1, 'smoke.sh no longer assigns SUDO="sudo"'
    assert guard.end() < sudo, (
        '`SUDO="sudo"` must stay inside the non-Darwin branch; outside it, Darwin '
        "would escalate again"
    )
    assert 'SUDO=""' in text, (
        "SUDO must still be initialised empty: `[ -z \"$SUDO\" ]` under `set -u` "
        "aborts when it is unset"
    )
    assert (
        "macOS: Docker Desktop maps bind mounts to the container uid; no sudo needed"
        in text
    ), "the Darwin branch must log why no escalation happens"
    assert not re.search(r"(?m)^\s*sudo\s+chown", text), (
        "no bare `sudo chown`: on Darwin $SUDO is empty, so an unguarded chown runs "
        "unprivileged and fails; keep `[ -z \"$SUDO\" ] || $SUDO chown ...`"
    )


def test_teardown_is_skipped_when_the_keep_flag_is_set():
    lines = _smoke_text().splitlines()
    teardown = [
        i
        for i, line in enumerate(lines, 1)
        if line.strip().startswith("docker compose") and "down -v" in line
    ]
    assert teardown, "the `compose ... down -v` teardown is gone"
    assert len(teardown) == 1, f"expected one `down -v` teardown; found {teardown}"
    guard_window = lines[max(0, teardown[0] - 5) : teardown[0] - 1]
    assert any("STANDALONE_SMOKE_KEEP" in line for line in guard_window), (
        "`down -v` must be guarded by STANDALONE_SMOKE_KEEP so a failed run can be "
        "inspected, and the guard belongs immediately before the teardown"
    )
    text = _smoke_text()
    assert KEEP_DEFAULT.search(text), (
        "the guard must default the variable (`${STANDALONE_SMOKE_KEEP:-0}`): a bare "
        '`[ "$STANDALONE_SMOKE_KEEP" = "1" ]` aborts under `set -u` and silently '
        "inverts the default, leaving every stack up"
    )
    assert INSPECT_COMMAND in text, (
        "the keep branch must print the exact command that shows the stack, "
        f"i.e. {INSPECT_COMMAND!r}"
    )
