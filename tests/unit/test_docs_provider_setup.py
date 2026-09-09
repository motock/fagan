"""Tests for the PP-05 documentation story: provider selection & authorization.

Scope: README.md and REFERENCE.md only (no production code). This story lands
after PP-01 (model_registry.json ships with no `roles`/`routing` block — an
operator must choose a provider) and PP-04 (scripts/install_checks.py probes
provider/forge *authorization*, not just binary presence).

These tests assert (1) the two supported ways to route a role to a provider
are documented, along with `PIPELINE_MODEL_REGISTRY_PATH` /
`model_registry.local.json`; (2) an authorization matrix names the exact
command that establishes each credential, matching
`scripts/install_checks.py`'s own hint strings verbatim, so the docs cannot
silently drift from what the code actually tells an operator to run;
(3) the Quickstart / Getting-started walkthrough no longer claim Ollama setup
is entirely skippable; (4) REFERENCE.md's example registry snippet no longer
shows the fictional `glm-4.7-flash:cloud` tag; (5) the 'Minimal configuration'
table reconciles its 'needs none of them' claim.

They are RED until README.md/REFERENCE.md are corrected.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
README = REPO_ROOT / "README.md"
REFERENCE = REPO_ROOT / "REFERENCE.md"
MODEL_REGISTRY_JSON = REPO_ROOT / "model_registry.json"

_INSTALL_CHECKS_PATH = REPO_ROOT / "scripts" / "install_checks.py"
_spec = importlib.util.spec_from_file_location("install_checks", str(_INSTALL_CHECKS_PATH))
install_checks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install_checks)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _readme_text() -> str:
    assert README.is_file(), "README.md must exist at the repo root"
    return README.read_text()


def _reference_text() -> str:
    assert REFERENCE.is_file(), "REFERENCE.md must exist at the repo root"
    return REFERENCE.read_text()


def _combined_text() -> str:
    return _readme_text() + "\n" + _reference_text()


def _normalized(text: str) -> str:
    """Collapse all whitespace runs to a single space, for matching prose
    fragments that may be soft-wrapped across multiple source lines."""
    return re.sub(r"\s+", " ", text)


def _h2_section_body(text: str, title: str) -> str:
    """Return the body of an H2 (## Title) section, heading line through the
    line before the next H2, skipping fenced-code-block false positives."""
    lines = text.splitlines()
    in_fence = False
    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith(f"## {title}") and start is None:
            start = i
            continue
        if start is not None and line.startswith("## "):
            end = i
            break
    assert start is not None, f"'## {title}' not found"
    return "\n".join(lines[start:end])


def _h3_section_body(text: str, title: str) -> str:
    """Return the body of an H3 (### Title) section, heading line through the
    line before the next H2 or H3, skipping fenced-code-block false positives."""
    lines = text.splitlines()
    in_fence = False
    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith(f"### {title}") and start is None:
            start = i
            continue
        if start is not None and (line.startswith("## ") or line.startswith("### ")):
            end = i
            break
    assert start is not None, f"'### {title}' not found"
    return "\n".join(lines[start:end])


def _first_fenced_json_block(body: str) -> str:
    lines = body.splitlines()
    in_block = False
    collected = []
    for line in lines:
        stripped = line.strip()
        if not in_block and stripped.startswith("```json"):
            in_block = True
            continue
        if in_block and stripped.startswith("```"):
            break
        if in_block:
            collected.append(line)
    assert collected, "no ```json fenced block found"
    return "\n".join(collected)


def _live_model_registry() -> dict:
    return json.loads(MODEL_REGISTRY_JSON.read_text())


# ---------------------------------------------------------------------------
# 1. Authorization matrix — exact commands, cross-checked against
#    scripts/install_checks.py's own hint strings (the single source of
#    truth for the remedy each auth probe reports).
# ---------------------------------------------------------------------------

def test_gh_auth_login_command_documented():
    combined = _combined_text()
    total = combined.count("gh auth login")
    assert total >= 1, (
        "README.md and REFERENCE.md combined must document the exact "
        "'gh auth login' command that authorizes the GitHub CLI"
    )


def test_claude_auth_login_command_documented():
    combined = _combined_text()
    total = combined.count("claude auth login")
    assert total >= 1, (
        "README.md and REFERENCE.md combined must document the exact "
        "'claude auth login' command that authorizes the Claude Code CLI"
    )


def test_ollama_signin_command_documented():
    combined = _combined_text()
    total = combined.count("ollama signin")
    assert total >= 1, (
        "README.md and REFERENCE.md combined must document the exact "
        "'ollama signin' command required for any ':cloud'-tagged model"
    )


@pytest.mark.parametrize("tool", ["gh", "claude", "ollama"])
def test_authorization_command_matches_install_checks_hint(tool):
    """The doc's remedy command must be byte-identical to the command named
    in install_checks.py's own hint for an unauthorized/missing tool - the
    docs must describe what the code actually tells an operator to run, not
    an approximation of it."""
    hint = install_checks._UNAUTHORIZED_HINTS[tool]
    command = hint.split("run: ", 1)[1]
    combined = _combined_text()
    assert command in combined, (
        f"README.md/REFERENCE.md must contain the exact command {command!r} "
        f"from install_checks.py's {tool!r} unauthorized-hint ({hint!r})"
    )


def test_gh_row_explains_pr_and_merge_need():
    combined = _combined_text()
    # Find every line mentioning the gh command and require at least one to
    # explain *why* (PR/merge path), not just the bare command.
    lines_with_command = [
        line for line in combined.splitlines() if "gh auth login" in line
    ]
    assert lines_with_command, "expected at least one line with 'gh auth login'"
    assert any(
        re.search(r"\bPR\b|merge", line, re.IGNORECASE) for line in lines_with_command
    ), (
        "the line(s) documenting 'gh auth login' must explain it is needed "
        "for the PR/merge path (mention 'PR' or 'merge'), found: "
        f"{lines_with_command}"
    )


def test_claude_row_explains_provider_claude_need():
    combined = _combined_text()
    lines_with_command = [
        line for line in combined.splitlines() if "claude auth login" in line
    ]
    assert lines_with_command, "expected at least one line with 'claude auth login'"
    assert any(
        re.search(r"\bclaude\b", line, re.IGNORECASE) for line in lines_with_command
    ), (
        "the line(s) documenting 'claude auth login' must name the "
        f"`claude` provider/backend, found: {lines_with_command}"
    )


def test_ollama_row_mentions_cloud_tag_suffix():
    combined = _combined_text()
    lines_with_command = [
        line for line in combined.splitlines() if "ollama signin" in line
    ]
    assert lines_with_command, "expected at least one line with 'ollama signin'"
    assert any(":cloud" in line for line in lines_with_command), (
        "the line(s) documenting 'ollama signin' must tie it to a "
        f"':cloud'-tagged model, found: {lines_with_command}"
    )


def test_ollama_cloud_proxy_note_present():
    """Brief requirement: state plainly that :cloud calls are proxied through
    ollama.com by the local daemon."""
    combined = _combined_text()
    assert "ollama.com" in combined, (
        "docs must mention ollama.com as the service :cloud tags are "
        "proxied through"
    )
    assert re.search(r"proxi", combined, re.IGNORECASE), (
        "docs must state that :cloud calls are *proxied* through ollama.com "
        "by the local daemon"
    )


def test_ollama_pipeline_sends_no_credential_of_its_own_note_present():
    """Brief requirement: state plainly that the pipeline sends no credential
    of its own for :cloud calls (the daemon sends its own)."""
    combined = _combined_text().lower()
    assert (
        "sends no credential" in combined
        or "sends none" in combined
        or "no credential of its own" in combined
    ), (
        "docs must state that the pipeline itself sends no credential for "
        "':cloud' calls - the local ollama daemon sends its own"
    )


def test_litellm_row_cross_references_provider_spec_not_duplicates_it():
    combined = _combined_text()
    assert "litellm" in combined.lower(), "docs must mention the litellm provider"
    assert "docs/specs/LITELLM_PROVIDER.md" in combined, (
        "the litellm authorization row must cross-reference "
        "docs/specs/LITELLM_PROVIDER.md rather than duplicating its content"
    )
    # Cross-reference, not duplication: the API-key security section header
    # from LITELLM_PROVIDER.md ("## 5. Security: API keys") should not be
    # copy-pasted verbatim into README/REFERENCE.
    assert "## 5. Security: API keys" not in combined, (
        "do not duplicate LITELLM_PROVIDER.md's content verbatim - link to it"
    )


def test_ondevice_only_tags_documented_as_needing_no_extra_authorization():
    combined = _combined_text().lower()
    assert "nothing extra" in combined or "no additional" in combined or "needs nothing" in combined, (
        "docs must state plainly that a purely on-device ollama/lmstudio/mlx "
        "tag (no ':cloud' suffix) needs no extra authorization"
    )


# ---------------------------------------------------------------------------
# 2. Provider selection is a required setup step: the two resolution
#    mechanisms, and the PIPELINE_MODEL_REGISTRY_PATH / model_registry.local.json
#    convention for keeping a personal registry out of the repo.
# ---------------------------------------------------------------------------

def test_registry_section_documents_env_var_mechanism():
    body = _h2_section_body(_reference_text(), "Per-role provider/model configuration")
    assert "PIPELINE_BACKEND_" in body, (
        "'Per-role provider/model configuration' must document the "
        "PIPELINE_BACKEND_<ROLE> env var mechanism"
    )


def test_registry_section_documents_roles_block_mechanism():
    body = _h2_section_body(_reference_text(), "Per-role provider/model configuration")
    assert "roles" in body, (
        "'Per-role provider/model configuration' must document the "
        "registry file's `roles` block mechanism"
    )


def test_registry_section_documents_model_registry_path_env():
    body = _h2_section_body(_reference_text(), "Per-role provider/model configuration")
    assert "PIPELINE_MODEL_REGISTRY_PATH" in body, (
        "'Per-role provider/model configuration' must document "
        "PIPELINE_MODEL_REGISTRY_PATH as the way to point at an alternate "
        "registry file"
    )


def test_registry_section_documents_local_json_convention():
    body = _h2_section_body(_reference_text(), "Per-role provider/model configuration")
    assert "model_registry.local.json" in body, (
        "'Per-role provider/model configuration' must document the "
        "gitignored model_registry.local.json convention for keeping a "
        "personal registry out of the repo"
    )


def test_model_registry_path_env_var_exists_in_code():
    """Guard against inventing an env var name: PIPELINE_MODEL_REGISTRY_PATH
    must be the literal name app/role_registry.py actually reads."""
    source = (REPO_ROOT / "app" / "role_registry.py").read_text()
    assert '_REGISTRY_PATH_ENV = "PIPELINE_MODEL_REGISTRY_PATH"' in source


def test_model_registry_local_json_is_actually_gitignored():
    """Guard against documenting a convention the repo doesn't enforce."""
    gitignore = (REPO_ROOT / ".gitignore").read_text()
    assert "model_registry.local.json" in gitignore, (
        "model_registry.local.json must actually be listed in .gitignore "
        "for the docs' claim about it staying out of the repo to hold"
    )


def test_resolve_role_provider_priority_order_documented_correctly():
    """The documented resolution order must match role_registry.resolve_role:
    plan role_config -> PIPELINE_BACKEND_<ROLE> env -> registry `roles` ->
    caller's fallback, in that order."""
    body = _h2_section_body(_reference_text(), "Per-role provider/model configuration")
    i_plan = body.find("role_config")
    i_env = body.find("PIPELINE_BACKEND_")
    i_registry = body.find("roles.")
    i_fallback = max(body.find("default"), body.find("fallback"))
    assert -1 not in (i_plan, i_env, i_registry, i_fallback), (
        "expected all four resolution-order markers (role_config, "
        "PIPELINE_BACKEND_<ROLE>, roles.<role>, and a default/fallback "
        f"marker) to be present in the section body; got indices "
        f"plan={i_plan} env={i_env} registry={i_registry} fallback={i_fallback}"
    )
    assert i_plan < i_env < i_registry < i_fallback, (
        "resolve_role's documented priority order must be plan role_config "
        "-> PIPELINE_BACKEND_<ROLE> env -> registry roles -> fallback, in "
        f"that order; got indices plan={i_plan} env={i_env} "
        f"registry={i_registry} fallback={i_fallback}"
    )


# ---------------------------------------------------------------------------
# 3. Stale example fixed: REFERENCE.md's registry snippet no longer shows
#    the fictional glm-4.7-flash:cloud tag, and every tag it does show is a
#    real tag from the merged providers catalog.
# ---------------------------------------------------------------------------

def test_glm_stale_tag_removed_from_reference():
    text = _reference_text()
    assert text.count("glm-4.7-flash") == 0, (
        "REFERENCE.md must not reference the fictional 'glm-4.7-flash' tag "
        "- it is not a tag this repo ships"
    )


def test_glm_stale_tag_absent_from_readme_too():
    text = _readme_text()
    assert text.count("glm-4.7-flash") == 0, (
        "README.md must not reference the fictional 'glm-4.7-flash' tag"
    )


def test_reference_example_registry_json_is_valid_json():
    body = _h2_section_body(_reference_text(), "Per-role provider/model configuration")
    block = _first_fenced_json_block(body)
    try:
        json.loads(block)
    except json.JSONDecodeError as exc:
        pytest.fail(
            "REFERENCE.md's example registry snippet under 'Per-role "
            f"provider/model configuration' is not valid JSON: {exc}"
        )


def test_reference_example_registry_tags_exist_in_merged_providers_catalog():
    """Every (provider, model, tag) triple shown in the doc's example
    registry snippet must correspond to a real entry in the merged
    model_registry.json - guards against inventing or leaving a stale tag."""
    body = _h2_section_body(_reference_text(), "Per-role provider/model configuration")
    block = _first_fenced_json_block(body)
    example = json.loads(block)
    live = _live_model_registry()

    mismatches = []
    for provider_name, provider_cfg in example.get("providers", {}).items():
        live_provider = live.get("providers", {}).get(provider_name)
        if live_provider is None:
            mismatches.append(f"provider {provider_name!r} not in model_registry.json")
            continue
        for model_name, model_cfg in provider_cfg.get("models", {}).items():
            live_model = live_provider.get("models", {}).get(model_name)
            if live_model is None:
                mismatches.append(
                    f"model {model_name!r} not declared under "
                    f"providers.{provider_name}.models in model_registry.json"
                )
                continue
            if live_model.get("tag") != model_cfg.get("tag"):
                mismatches.append(
                    f"providers.{provider_name}.models.{model_name}.tag: "
                    f"doc says {model_cfg.get('tag')!r}, "
                    f"model_registry.json says {live_model.get('tag')!r}"
                )
    assert not mismatches, (
        "REFERENCE.md's example registry snippet has entries that don't "
        "match the merged model_registry.json:\n" + "\n".join(mismatches)
    )


# ---------------------------------------------------------------------------
# 4. Quickstart / Getting-started walkthrough: correct the "claude is the
#    default, skip Ollama setup entirely" framing.
# ---------------------------------------------------------------------------

def test_quickstart_no_longer_claims_local_setup_entirely_skippable():
    normalized = _normalized(_readme_text())
    old_fragment = (
        "Skip every `PIPELINE_LOCAL_*`, `PIPELINE_BACKEND_*=ollama/lmstudio/mlx`, "
        "and Ollama/MLX/LM Studio setup entirely"
    )
    assert old_fragment not in normalized, (
        "README.md's Quickstart must no longer claim Ollama/MLX/LM Studio "
        "setup can be skipped entirely - PP-01 removed the shipped registry's "
        "role routing, so provider selection is now a conscious setup step "
        "(and even the default `claude` path needs `claude auth login`)"
    )


def test_walkthrough_no_longer_claims_no_local_model_required_anywhere():
    normalized = _normalized(_readme_text())
    old_fragment = (
        "No local model is required anywhere in this walkthrough: with "
        "`PIPELINE_BACKEND_DISPATCH=claude` (the default) dispatch and "
        "review shell out to the Claude Code CLI and never touch ollama."
    )
    assert old_fragment not in normalized, (
        "README.md's Getting-started walkthrough must no longer open with "
        "the blanket 'no local model is required anywhere' claim"
    )


def test_quickstart_section_mentions_provider_authorization():
    """The Quickstart (which includes the Getting-started walkthrough as an
    H3 subsection) must reference the authorization step for whichever
    provider is chosen."""
    body = _h2_section_body(_readme_text(), "Quickstart")
    assert (
        "auth login" in body or "ollama signin" in body or "auth status" in body
    ), (
        "README.md's Quickstart section must reference the provider "
        "authorization step (e.g. 'claude auth login', 'gh auth login', or "
        "'ollama signin')"
    )


# ---------------------------------------------------------------------------
# 5. 'Minimal configuration' table: add the provider-selection row and
#    reconcile the "needs none of them" claim.
# ---------------------------------------------------------------------------

def test_minimal_config_zero_setup_claim_is_reconciled():
    normalized = _normalized(_reference_text())
    assert "needs none of them" not in normalized, (
        "REFERENCE.md's 'Minimal configuration' section must no longer "
        "claim a first deployment 'needs none of them' unqualified - "
        "provider selection/authorization is now a required setup step "
        "even for the default `claude` backend"
    )


def test_minimal_config_table_gains_provider_selection_row():
    body = _h3_section_body(_reference_text(), "Minimal configuration")
    table_rows = [line for line in body.splitlines() if line.strip().startswith("|")]
    assert any(
        re.search(r"auth|provider", row, re.IGNORECASE) for row in table_rows
    ), (
        "REFERENCE.md's 'Minimal configuration' table must gain a row "
        "covering provider selection/authorization; found table rows: "
        f"{table_rows}"
    )


# ---------------------------------------------------------------------------
# 6. Boundary / sanity
# ---------------------------------------------------------------------------

def test_readme_and_reference_still_exist_and_nonempty():
    assert README.is_file() and README.read_text().strip() != ""
    assert REFERENCE.is_file() and REFERENCE.read_text().strip() != ""


def test_authorization_commands_appear_at_least_once_each_not_zero():
    """Explicit boundary case for the DONE CRITERION's grep -c checks: each
    required command must appear a positive number of times, not zero."""
    combined = _combined_text()
    for command in ("gh auth login", "claude auth login", "ollama signin"):
        count = combined.count(command)
        assert count >= 1, f"{command!r} must appear at least once, found {count}"


def test_glm_stale_tag_count_is_exactly_zero_in_reference():
    """Explicit boundary case mirroring the DONE CRITERION's exact grep -c
    expectation (0, not just 'reduced')."""
    count = _reference_text().count("glm-4.7-flash")
    assert count == 0, f"expected exactly 0 occurrences, found {count}"
