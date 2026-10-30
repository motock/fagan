"""The entrypoint loads operator credentials from mounted secret files.

``deploy/lib/operator_env.sh`` is the only thing that connects a mounted Docker
or Kubernetes secret to the environment the backend reads; the entrypoint
sources it before it starts anything. These tests source that library in a
subprocess and call its two entry points, so the behaviour under test is the
shipped shell code rather than a description of it.

The library exists instead of a branch inside the entrypoint for one reason:
there is no ``PIPELINE_ENTRYPOINT_SOURCE_ONLY``-style "stop before the process
start" switch anywhere in the production script. A switch is a fail-open: under
dash - ``/bin/sh`` in the Debian-slim image and on the Ubuntu CI runners - a
top-level ``return`` in an *executed* script stops it with status 0, so any
invocation that reached that branch (a rename in a derived image, a k8s
``command:`` override, a wrapper, a stray variable from a shared ``.env``)
would leave a container that looks healthy with the dashboard, the MCP
transport and the scheduler all absent. With the logic in a library there is
nothing to bypass and nothing to leak into a real container start.

Covered: a private, owned file is exported; a group/other accessible file, a
directory and a foreign-owned file are refused; a missing, empty or
whitespace-only file is not an error; an ambient credential is discarded rather
than trusted; ``GH_TOKEN`` implies the github forge; the repo defaults SA-05
relies on are set and a loose repos root fails closed; the production entrypoint
has no startup bypass, and an executed copy under any other name still exits
non-zero instead of reporting success from an idle container.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The shipped entrypoint and the library it sources. The credential logic lives
#: in the library so the smoke tests can exercise it without a skip-startup
#: branch in the script that runs in production.
ENTRYPOINT = REPO_ROOT / "deploy" / "entrypoint.sh"

TIMEOUT_SECONDS = 60

# The mount the entrypoint consults when PIPELINE_GIT_TOKEN_FILE is unset.
GH_SECRET_PATH = Path("/run/secrets/gh_token")

# Source the library the entrypoint sources, then run the caller's probe. It is
# sourced from the repository root so the relative path resolves in every
# runner. The library starts nothing, so no switch is needed to keep the test
# from spawning a service - and that is the point: the shipped script has no
# such switch to set.
SOURCE_PREFIX = (
    ". deploy/lib/operator_env.sh; load_operator_credentials; ensure_repos_root; "
)


def _base_env(tmp_path: Path) -> dict[str, str]:
    """A hermetic environment: no ambient credentials, no real /run/secrets."""
    plans = tmp_path / "plans"
    plans.mkdir()
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
        "HOME": str(tmp_path),
        "PIPELINE_IDENTITY_PROVIDER": "none-for-test",
        "PLAN_DIR": str(plans),
        "WORKTREE_ROOT": str(worktrees),
        "PIPELINE_REPOS_ROOT": str(tmp_path / "repos"),
        # Point every credential at a path that does not exist, so a stray
        # /run/secrets on the host can never leak into a test.
        "ANTHROPIC_API_KEY_FILE": str(tmp_path / "absent-anthropic"),
        "CLAUDE_CODE_OAUTH_TOKEN_FILE": str(tmp_path / "absent-oauth"),
        "GH_TOKEN_FILE": str(tmp_path / "absent-gh"),
        "OLLAMA_API_KEY_FILE": str(tmp_path / "absent-ollama"),
        "PIPELINE_GIT_TOKEN_FILE": str(tmp_path / "absent-git-token"),
    }


def _secret(path: Path, value: str, mode: int = 0o600) -> Path:
    path.write_text(value, encoding="utf-8")
    path.chmod(mode)
    return path


def _run(probe: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # -eu mirrors the entrypoint's own mode, so an unset-variable mistake in the
    # library surfaces here as the failure it would be in the container.
    return subprocess.run(
        ["sh", "-eu", "-c", SOURCE_PREFIX + probe],
        check=False,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )


def test_private_secret_file_is_exported_and_never_logged(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    value = "sk-ant-oat-secret-value-0123456789"
    env["ANTHROPIC_API_KEY_FILE"] = str(_secret(tmp_path / "anthropic", value + "\n"))

    result = _run("printenv PIPELINE_ANTHROPIC_API_KEY", env)

    assert result.returncode == 0, result.stderr
    # Trailing newline stripped, exact value preserved.
    assert result.stdout == value + "\n"
    assert value not in result.stderr
    assert "PIPELINE_ANTHROPIC_API_KEY" in result.stderr


def test_gh_token_from_file_implies_the_github_forge(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    env["GH_TOKEN_FILE"] = str(_secret(tmp_path / "gh", "ghp_example_token\n"))

    result = _run("printenv PIPELINE_FORGE", env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "github"
    assert "ghp_example_token" not in result.stderr


def test_preset_forge_is_kept(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    env["GH_TOKEN_FILE"] = str(_secret(tmp_path / "gh", "ghp_example_token\n"))
    env["PIPELINE_FORGE"] = "gitlab"

    result = _run("printenv PIPELINE_FORGE", env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "gitlab"


def test_git_token_file_follows_the_mounted_gh_token_or_stays_unset(
    tmp_path: Path,
) -> None:
    # The default is /run/secrets/gh_token, and the entrypoint only adopts it
    # when the mount actually exists. Assert the branch that applies on this
    # host instead of accepting either outcome, which would pass unconditionally
    # and never exercise the security-relevant default.
    if GH_SECRET_PATH.exists():
        # A developer or CI host with a real mount at the default path: never
        # assert on that path's contents or substitute it. The branch is covered
        # by the preset-git-token-file test and by the real entrypoint on any
        # host without the mount.
        pytest.skip(f"{GH_SECRET_PATH} exists on this host; not probing a real mount")
    env = _base_env(tmp_path)
    del env["PIPELINE_GIT_TOKEN_FILE"]

    result = _run(
        'printenv PIPELINE_GIT_TOKEN_FILE || echo __UNSET__',
        env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "__UNSET__"


def test_preset_git_token_file_is_kept(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    preset = tmp_path / "operator-git-token"
    env["PIPELINE_GIT_TOKEN_FILE"] = str(preset)

    result = _run("printenv PIPELINE_GIT_TOKEN_FILE", env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(preset)


def test_group_readable_secret_file_is_refused_without_echoing_it(
    tmp_path: Path,
) -> None:
    env = _base_env(tmp_path)
    value = "ghp_leaked_value_must_not_appear"
    env["GH_TOKEN_FILE"] = str(_secret(tmp_path / "gh", value + "\n", mode=0o644))

    result = _run("echo __REACHED__", env)

    assert result.returncode != 0
    assert "__REACHED__" not in result.stdout
    assert "GH_TOKEN" in result.stderr
    assert value not in result.stderr
    assert value not in result.stdout


def test_missing_secret_file_is_not_an_error(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    env["OLLAMA_API_KEY_FILE"] = str(tmp_path / "no-such-ollama-key")

    result = _run("printenv OLLAMA_API_KEY || echo __UNSET__", env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "__UNSET__"


def test_empty_secret_file_is_not_an_error(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    empty = tmp_path / "empty-ollama"
    empty.write_text("", encoding="utf-8")
    empty.chmod(0o600)
    env["OLLAMA_API_KEY_FILE"] = str(empty)

    result = _run("printenv OLLAMA_API_KEY || echo __UNSET__", env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "__UNSET__"


def test_whitespace_only_secret_file_is_not_an_error(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    blank = tmp_path / "blank-oauth"
    blank.write_text(" \n\t\n", encoding="utf-8")
    blank.chmod(0o600)
    env["CLAUDE_CODE_OAUTH_TOKEN_FILE"] = str(blank)

    result = _run("printenv CLAUDE_CODE_OAUTH_TOKEN || echo __UNSET__", env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "__UNSET__"


def test_allowed_repo_roots_defaults_to_the_repos_root(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    repos = tmp_path / "repos"

    result = _run("printenv PIPELINE_ALLOWED_REPO_ROOTS", env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(repos)
    assert repos.is_dir()


def test_preset_allowed_repo_roots_is_kept(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    preset = tmp_path / "checkouts"
    env["PIPELINE_ALLOWED_REPO_ROOTS"] = str(preset)

    result = _run("printenv PIPELINE_ALLOWED_REPO_ROOTS", env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(preset)


def test_ambient_credentials_are_not_trusted(tmp_path: Path) -> None:
    # The documented invariant is that a credential never comes from an
    # environment value. An ambient GH_TOKEN/OLLAMA_API_KEY with no mounted file
    # must therefore be discarded, not exported and used to infer a forge.
    env = _base_env(tmp_path)
    env["GH_TOKEN"] = "ghp_ambient_value"
    env["OLLAMA_API_KEY"] = "ambient-ollama-value"

    result = _run(
        "printenv GH_TOKEN || echo __NO_GH__; "
        "printenv OLLAMA_API_KEY || echo __NO_OLLAMA__; "
        "printenv PIPELINE_FORGE || echo __NO_FORGE__",
        env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["__NO_GH__", "__NO_OLLAMA__", "__NO_FORGE__"]
    assert "ghp_ambient_value" not in result.stderr
    assert "ambient-ollama-value" not in result.stderr


def test_directory_in_place_of_a_secret_file_is_refused(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    env["GH_TOKEN_FILE"] = str(directory)

    result = _run("echo __REACHED__", env)

    assert result.returncode != 0
    assert "__REACHED__" not in result.stdout
    assert "GH_TOKEN" in result.stderr


def test_symlink_in_place_of_a_secret_file_is_refused(tmp_path: Path) -> None:
    # stat() follows links, so without an explicit refusal any 0600 file this uid
    # owns could be re-pointed at by a *_FILE variable and loaded under another
    # credential's name (e.g. the OIDC client secret read as a model key).
    env = _base_env(tmp_path)
    target = _secret(tmp_path / "real-secret", "sk-ant-symlinked-value")
    link = tmp_path / "link-to-secret"
    link.symlink_to(target)
    env["ANTHROPIC_API_KEY_FILE"] = str(link)

    result = _run("echo __REACHED__", env)

    assert result.returncode != 0
    assert "__REACHED__" not in result.stdout
    assert "PIPELINE_ANTHROPIC_API_KEY" in result.stderr
    assert "symlink" in result.stderr
    assert "sk-ant-symlinked-value" not in result.stderr


def test_secret_file_owned_by_another_user_is_refused(tmp_path: Path) -> None:
    # Ownership cannot be faked without a second uid (root only), so report a
    # foreign owner from `stat` and keep every other behaviour real.
    real_stat = shutil.which("stat")
    assert real_stat is not None
    env = _base_env(tmp_path)
    value = "sk-ant-oat-owned-elsewhere"
    env["ANTHROPIC_API_KEY_FILE"] = str(_secret(tmp_path / "anthropic", value))
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "stat"
    shim.write_text(
        "#!/bin/sh\n"
        'case "$1:$2" in\n'
        '  -c:%u) echo 424242; exit 0 ;;\n'
        "esac\n"
        f'exec "{real_stat}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    env["PATH"] = f"{shim_dir}:{env['PATH']}"

    result = _run("echo __REACHED__", env)

    assert result.returncode != 0
    assert "__REACHED__" not in result.stdout
    assert "PIPELINE_ANTHROPIC_API_KEY" in result.stderr
    assert value not in result.stderr
    assert value not in result.stdout


def test_existing_repos_root_is_tightened_to_0750(tmp_path: Path) -> None:
    # A pre-existing root is the clone trust boundary, so group/other access is
    # removed before anything clones into it.
    env = _base_env(tmp_path)
    repos = tmp_path / "loose-repos"
    repos.mkdir()
    repos.chmod(0o777)
    env["PIPELINE_REPOS_ROOT"] = str(repos)

    result = _run("echo __REACHED__", env)

    assert result.returncode == 0, result.stderr
    assert "__REACHED__" in result.stdout
    # 0750 is the intended mode: group read/traverse is fine, group write and
    # all "other" access is not.
    assert repos.stat().st_mode & 0o027 == 0


def test_repos_root_that_stays_loose_is_refused(tmp_path: Path) -> None:
    # A root we cannot tighten (e.g. owned by another uid) fails closed: a
    # group/other writable root lets any process in the container plant a
    # repository an agent then runs.
    env = _base_env(tmp_path)
    repos = tmp_path / "unfixable-repos"
    repos.mkdir()
    repos.chmod(0o777)
    env["PIPELINE_REPOS_ROOT"] = str(repos)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "chmod"
    shim.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    shim.chmod(0o755)
    env["PATH"] = f"{shim_dir}:{env['PATH']}"

    result = _run("echo __REACHED__", env)

    assert result.returncode != 0
    assert "__REACHED__" not in result.stdout
    assert "PIPELINE_REPOS_ROOT" in result.stderr


def test_symlinked_repos_root_is_refused_before_anything_is_chmoded(
    tmp_path: Path,
) -> None:
    # PIPELINE_REPOS_ROOT is operator-supplied and may point at a bind mount, so
    # tightening its mode mutates whatever path it resolves to. A symlink would
    # let a typo (or a planted link) chmod an unrelated tree, so the link itself
    # is refused and the target keeps its mode.
    env = _base_env(tmp_path)
    target = tmp_path / "unrelated-tree"
    target.mkdir()
    target.chmod(0o777)
    link = tmp_path / "repos-link"
    link.symlink_to(target, target_is_directory=True)
    env["PIPELINE_REPOS_ROOT"] = str(link)

    result = _run("echo __REACHED__", env)

    assert result.returncode != 0
    assert "__REACHED__" not in result.stdout
    assert "PIPELINE_REPOS_ROOT" in result.stderr
    assert "symlink" in result.stderr
    # The path the link resolved to is untouched: no chmod through the link.
    assert target.stat().st_mode & 0o777 == 0o777


def test_repos_root_that_cannot_be_created_is_refused(tmp_path: Path) -> None:
    # Creation used to warn and carry on, which left PIPELINE_ALLOWED_REPO_ROOTS
    # pointing at a root that does not exist - a silent fail-open on the clone
    # trust boundary. The parent here is a regular file, so mkdir -p fails even
    # for a root user running the suite in a container.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n", encoding="utf-8")
    env = _base_env(tmp_path)
    env["PIPELINE_REPOS_ROOT"] = str(blocker / "repos")

    result = _run("echo __REACHED__", env)

    assert result.returncode != 0
    assert "__REACHED__" not in result.stdout
    assert "PIPELINE_REPOS_ROOT" in result.stderr


#: Every credential this story loads and the consumer that may strip the carrier
#: before a child starts. Stripping itself lives in `pipeline/execution.py` and
#: `app/backend_claude.py`, which are outside this story's scope (SA-09 and SF-03
#: own them). What this story can and must guarantee from `deploy/` is that the
#: operator is told, per credential, which consumer strips it - so the README
#: names one for every exported carrier and does not claim a strip that does not
#: exist today.
EXPORTED_CARRIERS = (
    "PIPELINE_ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "GH_TOKEN",
    "OLLAMA_API_KEY",
)


def test_readme_documents_every_exported_carrier() -> None:
    text = (REPO_ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    start = text.index("### Credentials")
    section = text[start:]

    missing = [name for name in EXPORTED_CARRIERS if name not in section]
    assert missing == [], (
        f"the Credentials section must name the variable each secret file becomes, "
        f"missing {missing!r}"
    )
    # The consumer of the one carrier a deploy-side control can point at today:
    # the agent-child strip in pipeline/execution.py. If that moves or stops
    # covering GH_TOKEN, the README has to move with it.
    assert "pipeline/execution.py" in section, (
        "the Credentials section must name the consumer that strips a carrier "
        "from an agent child (pipeline/execution.py::_AGENT_SECRET_ENV)"
    )


def test_readme_does_not_claim_a_strip_the_deploy_layer_does_not_have() -> None:
    text = (REPO_ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    section = text[text.index("### Credentials"):]

    # The model keys and the OAuth token reach agent children today: SA-09 (the
    # CLI carrier) and SF-03 (the agent denylist) own the strips. The section
    # must say so instead of implying the loader closes it.
    assert "tracked risk" in section, (
        "the Credentials section must record the remaining exposure as a tracked "
        "risk, not present the loader as having closed it"
    )
    assert "never handed to a model-driven shell" not in section, (
        "that claim was only true while the out-of-scope strips existed (SA-09 / "
        "SF-03); deploy/README.md must not assert it"
    )


def _posix_shells() -> list[str]:
    """Every POSIX shell on this host, deduplicated by real path.

    dash has to be in the list: it is /bin/sh on the Debian-slim image and on
    the Ubuntu CI runners, and its top-level `return` semantics are why the
    production script must not contain a "stop before startup" branch at all.
    Testing an executed copy under bash alone would prove nothing about the
    image.
    """
    shells: list[str] = []
    seen: set[str] = set()
    for candidate in ("dash", "bash", "sh"):
        found = shutil.which(candidate)
        if not found:
            continue
        real = os.path.realpath(found)
        if real in seen:
            continue
        seen.add(real)
        shells.append(found)
    return shells


#: Populated at import so the parametrised security tests cover every shell on
#: the host. `test_the_shell_matrix_is_not_empty` fails loudly when this is
#: empty: a silently empty matrix would turn the tests below into a no-op.
POSIX_SHELLS = _posix_shells()


def test_image_ships_the_sourced_loader_outside_the_runtime_uid() -> None:
    # The loader runs with every mounted secret in scope at start-up. The image
    # runs as uid 10001 - the uid agent children run as - so a library owned by
    # that uid is rewritable by code that already has execution: an agent could
    # edit it and have its own code re-run, with the secrets, at the next
    # container start. Root-owned and read-only is the deploy-side control that
    # closes that persistence path without touching the runtime.
    text = (REPO_ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    copy_lines = [
        line for line in text.splitlines() if "deploy/lib/operator_env.sh" in line
    ]
    assert copy_lines, "the image must copy deploy/lib/operator_env.sh"
    assert all("--chown=0:0" in line for line in copy_lines if line.startswith("COPY")), (
        f"the sourced credential loader must be root-owned, got {copy_lines!r}"
    )
    assert "chmod 0444 ./deploy/lib/operator_env.sh" in text, (
        "the sourced credential loader must be read-only for the runtime uid"
    )


def test_the_shell_matrix_is_not_empty() -> None:
    assert POSIX_SHELLS, (
        "no POSIX shell found on this host: the executed-entrypoint regression "
        "tests would silently pass without running anything"
    )


def test_entrypoint_has_no_startup_bypass() -> None:
    # The library holds the credential logic; the entrypoint must contain no
    # early exit and no test switch. A top-level `return` is not a safe way to
    # express one either: under dash an executed script stops with status 0.
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert "PIPELINE_ENTRYPOINT_SOURCE_ONLY" not in text, (
        "the production entrypoint must not read a test-only skip-startup switch: "
        "a stray value in a shared .env would leave an idle container that looks healthy"
    )
    early_returns = [line for line in text.splitlines() if re.match(r"\s*return\b", line)]
    assert early_returns == [], (
        "the production entrypoint must not return early; start-up assertions must "
        f"run in every invocation, found {early_returns!r}"
    )


@pytest.mark.parametrize("shell", POSIX_SHELLS)
def test_executed_copy_under_another_name_never_exits_zero(
    shell: str, tmp_path: Path
) -> None:
    # The exact case a name-based hook got wrong: the script copied or renamed
    # to something else (a derived image, a k8s `command:` override, a wrapper)
    # and executed under dash. With the logic in a library the copy cannot find
    # its library next to it, so it must fail closed - non-zero, no service
    # started - rather than `return 0` into an idle container. A leaked
    # PIPELINE_ENTRYPOINT_SOURCE_ONLY from a shared environment must change
    # nothing, because the shipped script no longer reads it.
    env = _base_env(tmp_path)
    env["PIPELINE_ENTRYPOINT_SOURCE_ONLY"] = "1"
    copy = tmp_path / "pipeline-server"
    copy.write_bytes(ENTRYPOINT.read_bytes())
    copy.chmod(0o755)

    result = subprocess.run(
        [shell, str(copy)],
        check=False,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )

    assert result.returncode != 0, (
        f"{shell}: an executed copy of the entrypoint exited 0 without starting "
        f"anything; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "starting dashboard" not in result.stderr
    assert "starting MCP transport" not in result.stderr


@pytest.mark.parametrize("shell", POSIX_SHELLS)
def test_executed_entrypoint_reaches_its_startup_checks(
    shell: str, tmp_path: Path
) -> None:
    # Positive proof that the shipped script, executed normally, runs its
    # start-up assertions instead of falling through an early `return`: with a
    # missing state directory it dies before the first process start. Nothing
    # is spawned here, so this is safe on every shell in the matrix.
    env = _base_env(tmp_path)
    env["PLAN_DIR"] = str(tmp_path / "no-such-plans")
    env["PIPELINE_ENTRYPOINT_SOURCE_ONLY"] = "1"

    result = subprocess.run(
        [shell, "deploy/entrypoint.sh"],
        check=False,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )

    assert result.returncode != 0
    assert "state directory is missing" in result.stderr
    assert "starting dashboard" not in result.stderr
