"""Tests for the notification / outbound-e-mail documentation story.

Scope: REFERENCE.md and .pipeline.env.example only (no production code).
The pipeline gained two externally visible behaviours the docs must now
describe:

1. A ``plan_completed`` notification that fires when every story in a plan
   reaches done -- exactly once per plan, with the once-only guard being
   the ``<plan>.plan_completed`` marker file in PLAN_DIR.
2. An opt-in outbound e-mail channel backed by the ``<plan>.outbox.jsonl``
   spool (disabled by default, event allowlist), whose send happens in the
   scheduler tick's drain phase -- never inline in the notification path,
   per REFERENCE.md's existing sink rule 2 -- and where a failed send
   retains the record for the next drain.

The docs must name all ten new env vars (the two ``PIPELINE_NOTIFY_OUTBOX_*``
vars and the eight ``PIPELINE_NOTIFY_EMAIL_*`` vars) with their defaults;
``.pipeline.env.example`` must carry them commented out with a per-var
default comment and an explicit credential warning for the password var.
Neither file may leak a plausible real credential, host, or address.

These tests are RED until the two doc files are updated. They read only
markdown/env-example text, so they run fine before any implementation
exists.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REFERENCE = REPO_ROOT / "REFERENCE.md"
ENV_EXAMPLE = REPO_ROOT / ".pipeline.env.example"

OUTBOX_VARS = (
    "PIPELINE_NOTIFY_OUTBOX_ENABLED",
    "PIPELINE_NOTIFY_OUTBOX_EVENTS",
)
EMAIL_VARS = (
    "PIPELINE_NOTIFY_EMAIL_ENABLED",
    "PIPELINE_NOTIFY_EMAIL_HOST",
    "PIPELINE_NOTIFY_EMAIL_PORT",
    "PIPELINE_NOTIFY_EMAIL_USER",
    "PIPELINE_NOTIFY_EMAIL_PASSWORD",
    "PIPELINE_NOTIFY_EMAIL_FROM",
    "PIPELINE_NOTIFY_EMAIL_TO",
    "PIPELINE_NOTIFY_EMAIL_TIMEOUT",
)
ALL_NOTIFY_VARS = OUTBOX_VARS + EMAIL_VARS

# Reviewer-mandated docs-vs-code consistency guard: the e-mail var names the
# docs may document are exactly the ones pipeline/notification_email.py
# actually reads from os.environ at call sites (comments and docstrings do
# not count).  If the code grows or drops a knob, this fails CI until the
# docs and this list are updated together.
_EMAIL_MODULE = REPO_ROOT / "pipeline" / "notification_email.py"
_CODE_EMAIL_VARS = set(
    re.findall(
        r"os\.environ\.get\(\s*\"(PIPELINE_NOTIFY_EMAIL_[A-Z_]+)\"",
        _EMAIL_MODULE.read_text(),
    )
)
assert _CODE_EMAIL_VARS == set(EMAIL_VARS), (
    "EMAIL_VARS drifted from pipeline/notification_email.py: the module "
    f"reads {sorted(_CODE_EMAIL_VARS)} but the test pins {sorted(EMAIL_VARS)}"
)

PASSWORD_VAR = "PIPELINE_NOTIFY_EMAIL_PASSWORD"
HOST_VAR = "PIPELINE_NOTIFY_EMAIL_HOST"

STALE_SENTENCE = (
    "No outbound sinks (Slack, webhook, email) are implemented; "
    "notifications are only written locally and consumed by the dashboard."
)
STALE_FRAGMENT = "No outbound sinks"

# Tokens that mark a value as an obvious placeholder rather than a real
# credential, host, or address.
PLACEHOLDER_TOKENS = (
    "example",
    "changeme",
    "change-me",
    "your",
    "placeholder",
    "app-password",
    "app password",
    "app_password",
    "replace",
    "dummy",
    "sample",
    "xxxx",
    "todo",
    "insert",
    "<",
    ">",
    "...",
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_MAIL_PROVIDER_RE = re.compile(
    r"gmail|googlemail|outlook|hotmail|yahoo|icloud|fastmail|proton",
    re.IGNORECASE,
)
_TOPIC_TITLE_RE = re.compile(r"notif|e-?mail|outbox|sink", re.IGNORECASE)
_ASSIGN_RE = re.compile(
    r"^\s*(?P<comment>#\s*)?(?:export\s+)?"
    r"(?P<name>[A-Z0-9_]+)\s*=\s*(?P<value>.*)$"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reference_text() -> str:
    assert REFERENCE.is_file(), "REFERENCE.md must exist at the repo root"
    return REFERENCE.read_text()


def _env_example_text() -> str:
    assert ENV_EXAMPLE.is_file(), (
        ".pipeline.env.example must exist at the repo root"
    )
    return ENV_EXAMPLE.read_text()


def _normalized(text: str) -> str:
    """Collapse whitespace runs so soft-wrapped prose can be matched."""
    return re.sub(r"\s+", " ", text)


def _h2_section_body(text: str, title: str) -> str:
    """Body of an H2 (## Title) section: heading line through the line
    before the next H2, skipping fenced-code-block false positives."""
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


def _h2_sections(text: str) -> list:
    """All H2 sections as (title, body) pairs, fence-aware."""
    lines = text.splitlines()
    sections = []
    in_fence = False
    current = None
    for i, line in enumerate(lines):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith("## "):
            if current is not None:
                sections.append((current[0], "\n".join(lines[current[1]:i])))
            current = (line[3:].strip(), i)
    if current is not None:
        sections.append((current[0], "\n".join(lines[current[1]:])))
    return sections


def _notification_section_bodies() -> list:
    """Bodies of every H2 section whose heading covers the notification /
    e-mail / outbox topic."""
    bodies = [
        body
        for title, body in _h2_sections(_reference_text())
        if _TOPIC_TITLE_RE.search(title)
    ]
    assert bodies, (
        "REFERENCE.md must have a `##` section whose heading covers the "
        "notification / e-mail / outbox behaviour (e.g. '## Notifications: "
        "plan_completed and the outbound e-mail channel')"
    )
    return bodies


def _plan_completed_section_body() -> str:
    """The new H2 section that documents both the plan_completed
    notification and the outbox.jsonl spool."""
    candidates = [
        body
        for body in _notification_section_bodies()
        if "plan_completed" in body and "outbox.jsonl" in body
    ]
    assert candidates, (
        "REFERENCE.md must gain a `##` section (heading mentioning "
        "notification/e-mail/outbox) whose body documents BOTH the "
        "`plan_completed` notification AND the `<plan>.outbox.jsonl` spool"
    )
    return candidates[0]


def _assignments(text: str, var: str) -> list:
    """(lineno, raw_value, is_commented) for lines that assign `var`."""
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        match = _ASSIGN_RE.match(line)
        if match and match.group("name") == var:
            out.append(
                (lineno, match.group("value").strip(), bool(match.group("comment")))
            )
    return out


def _password_values(text: str) -> list:
    """(lineno, raw_value) for any line mentioning the password var with an
    '=' after it (commented or not)."""
    values = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if PASSWORD_VAR not in line or "=" not in line:
            continue
        after = line.split(PASSWORD_VAR, 1)[1]
        if "=" not in after:
            continue
        values.append((lineno, after.split("=", 1)[1].strip()))
    return values


def _entry_line_index(lines: list, var: str):
    """Index of the line that introduces `var` (its assignment line if any,
    else its first mention)."""
    for i, line in enumerate(lines):
        match = _ASSIGN_RE.match(line)
        if match and match.group("name") == var:
            return i
    for i, line in enumerate(lines):
        if var in line:
            return i
    return None


def _comment_block_above(lines: list, idx: int, var: str) -> list:
    """Contiguous comment lines (and blanks) immediately above line `idx`,
    stopping at any line that introduces a *different* PIPELINE_NOTIFY_*
    var, so each var's own comment block is isolated."""
    block = []
    j = idx - 1
    while j >= 0:
        line = lines[j]
        if any(other in line for other in ALL_NOTIFY_VARS if other != var):
            break
        if line.strip() == "":
            j -= 1
            continue
        if line.lstrip().startswith("#"):
            block.append(line)
            j -= 1
            continue
        break
    block.reverse()
    return block


def _looks_like_real_secret(value: str) -> bool:
    """Heuristic: a non-empty value with no placeholder marker and 8+
    placeholder-free word characters looks like a real credential."""
    v = value.strip().strip("'\"").strip()
    if not v:
        return False
    low = v.lower()
    if any(tok in low for tok in PLACEHOLDER_TOKENS):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_.\-]{8,}", v))


def _non_example_emails(text: str) -> list:
    bad = []
    for match in _EMAIL_RE.finditer(text):
        domain = match.group(0).split("@", 1)[1].lower()
        if domain != "example.com" and not domain.endswith(".example.com"):
            bad.append(match.group(0))
    return bad


# ---------------------------------------------------------------------------
# 1. The ten new env var names are documented (membership, individually).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("var", ALL_NOTIFY_VARS)
def test_env_var_documented_in_reference(var):
    assert var in _reference_text(), (
        f"REFERENCE.md must document {var} (name, default, and effect)"
    )


@pytest.mark.parametrize("var", ALL_NOTIFY_VARS)
def test_env_var_present_in_env_example(var):
    assert var in _env_example_text(), (
        f".pipeline.env.example must list {var} (commented out)"
    )


def test_reference_mentions_plan_completed():
    assert "plan_completed" in _reference_text(), (
        "REFERENCE.md must document the plan_completed notification"
    )


def test_reference_mentions_outbox_jsonl():
    assert "outbox.jsonl" in _reference_text(), (
        "REFERENCE.md must document the <plan>.outbox.jsonl spool file"
    )


# ---------------------------------------------------------------------------
# 2. The new ## section exists and names every env var with its default.
# ---------------------------------------------------------------------------

def test_new_section_documents_plan_completed_and_outbox():
    body = _plan_completed_section_body()
    assert "plan_completed" in body and "outbox.jsonl" in body


@pytest.mark.parametrize("var", ALL_NOTIFY_VARS)
def test_new_section_names_each_env_var(var):
    body = _normalized(_plan_completed_section_body())
    assert var in body, (
        f"the new notification/e-mail ## section must name {var} with its "
        f"default; section body starts with: {body[:600]!r}"
    )


def test_new_section_mentions_defaults():
    body = _plan_completed_section_body()
    assert re.search(r"default", body, re.IGNORECASE), (
        "the new ## section must give each of the ten env vars' default"
    )


# ---------------------------------------------------------------------------
# 3. plan_completed semantics: when it fires, once-only guard, marker file.
# ---------------------------------------------------------------------------

def test_plan_completed_fires_when_every_story_reaches_done():
    body = _normalized(_plan_completed_section_body()).lower()
    assert re.search(r"every story|all stories|each story", body), (
        "the section must say the notification fires when every story in "
        "the plan reaches done"
    )
    assert re.search(r"\bdone\b", body), (
        "the section must tie the trigger to stories reaching 'done'"
    )


def test_plan_completed_fires_exactly_once_per_plan():
    body = _normalized(_plan_completed_section_body()).lower()
    assert "exactly once" in body, (
        "the section must state the notification fires exactly once per plan"
    )


def test_once_only_guard_is_marker_file_in_plan_dir():
    body = _normalized(_plan_completed_section_body())
    assert ".plan_completed" in body, (
        "the section must name the <plan>.plan_completed marker file as "
        "the once-only guard"
    )
    assert "PLAN_DIR" in body, (
        "the section must say the marker file lives in PLAN_DIR"
    )


# ---------------------------------------------------------------------------
# 4. Outbox sink semantics: spool, default-off, allowlist, drain phase.
# ---------------------------------------------------------------------------

def test_outbox_spool_documented():
    body = _normalized(_plan_completed_section_body()).lower()
    assert "spool" in body, (
        "the section must describe <plan>.outbox.jsonl as the outbox spool"
    )


def test_outbox_disabled_by_default():
    body = _normalized(_plan_completed_section_body()).lower()
    assert re.search(r"disabled by default|off by default", body), (
        "the section must say the outbox/e-mail sink is disabled by default"
    )


def test_outbox_event_allowlist_documented():
    body = _normalized(_plan_completed_section_body()).lower()
    assert re.search(r"allow.?list", body), (
        "the section must describe the event-allowlist behaviour "
        "(PIPELINE_NOTIFY_OUTBOX_EVENTS)"
    )


def test_send_happens_in_drain_phase_not_inline():
    body = _normalized(_plan_completed_section_body()).lower()
    assert "drain" in body, "the section must mention the drain phase"
    assert "scheduler" in body, (
        "the section must tie the send to the scheduler tick's drain phase"
    )
    assert re.search(
        r"not\s+inline|rather than\s+inline|instead of\s+inline", body
    ), (
        "the section must state the send is NOT inline in the notification "
        "path (it happens in the drain phase)"
    )


def test_drain_rationale_cites_sink_rule_2():
    body = _normalized(_plan_completed_section_body()).lower()
    assert re.search(r"rule\s*#?\s*2\b", body), (
        "the section must explain WHY the send is deferred to the drain "
        "phase by citing REFERENCE.md's existing sink rule 2"
    )


def test_failed_send_retains_record_for_next_drain():
    body = _normalized(_plan_completed_section_body()).lower()
    assert re.search(r"retain|kept|keep", body), (
        "the section must say a failed send retains the record"
    )
    assert re.search(
        r"next drain|following drain|subsequent drain|later drain", body
    ), "the section must say the retained record is sent on the next drain"


# ---------------------------------------------------------------------------
# 5. The stale 'No outbound sinks' sentence is gone.
# ---------------------------------------------------------------------------

def test_stale_no_outbound_sinks_sentence_removed():
    normalized = _normalized(_reference_text())
    assert STALE_SENTENCE not in normalized, (
        "REFERENCE.md still says outbound sinks are not implemented - the "
        "opt-in e-mail channel makes that sentence false"
    )
    assert STALE_FRAGMENT not in normalized, (
        "REFERENCE.md must not claim 'No outbound sinks' any more"
    )


# ---------------------------------------------------------------------------
# 6. .pipeline.env.example structure: commented out, defaults, warning.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("var", ALL_NOTIFY_VARS)
def test_env_example_var_is_commented_out(var):
    lines = _env_example_text().splitlines()
    hits = [line for line in lines if var in line]
    assert hits, f"{var} must appear in .pipeline.env.example"
    uncommented = [line for line in hits if not line.lstrip().startswith("#")]
    assert not uncommented, (
        f"{var} is opt-in: every occurrence in .pipeline.env.example must "
        f"be commented out; uncommented occurrences: {uncommented}"
    )


@pytest.mark.parametrize("var", ALL_NOTIFY_VARS)
def test_env_example_var_comment_states_default(var):
    lines = _env_example_text().splitlines()
    idx = _entry_line_index(lines, var)
    assert idx is not None, f"{var} missing from .pipeline.env.example"
    block = _comment_block_above(lines, idx, var) + [lines[idx]]
    assert "default" in " ".join(block).lower(), (
        f"the comment accompanying {var} must state its default and effect; "
        f"comment block was:\n" + "\n".join(block)
    )


def test_password_var_has_explicit_credential_warning():
    lines = _env_example_text().splitlines()
    idx = _entry_line_index(lines, PASSWORD_VAR)
    assert idx is not None, f"{PASSWORD_VAR} missing from .pipeline.env.example"
    raw_block = _comment_block_above(lines, idx, PASSWORD_VAR) + [lines[idx]]
    block = _normalized(" ".join(raw_block)).lower()
    required = {
        "credential": "credential" in block,
        "app-password guidance": (
            "app-password" in block or "app password" in block
        ),
        "never": "never" in block,
        "primary account": "primary" in block,
        "gitignored": "gitignore" in block,
        "override": "override" in block,
        "launchd plist": "launchd" in block,
        "~/.claude.json": "claude.json" in block,
    }
    missing = [name for name, ok in required.items() if not ok]
    assert not missing, (
        f"the {PASSWORD_VAR} comment block must warn that it is a credential "
        "(use a provider app-password, never a primary account password) and "
        "note that .pipeline.env is gitignored and its values OVERRIDE the "
        f"launchd plist and the ~/.claude.json MCP env block; missing: "
        f"{missing}; block was:\n" + "\n".join(raw_block)
    )


# ---------------------------------------------------------------------------
# 7. Credential hygiene: no real secrets, hosts, or addresses anywhere.
# ---------------------------------------------------------------------------

def test_no_uncommented_password_assignment_with_value():
    for lineno, raw, commented in _assignments(_env_example_text(), PASSWORD_VAR):
        if not commented and raw:
            pytest.fail(
                f".pipeline.env.example:{lineno} has an uncommented "
                f"{PASSWORD_VAR}= assignment with a non-empty value; it must "
                "stay commented out and must never carry a real credential"
            )


@pytest.mark.parametrize(
    "path", [REFERENCE, ENV_EXAMPLE], ids=["REFERENCE.md", "env-example"]
)
def test_no_plausible_real_password_value(path):
    bad = [
        (lineno, raw)
        for lineno, raw in _password_values(path.read_text())
        if _looks_like_real_secret(raw)
    ]
    assert not bad, (
        f"{path.name} must not contain a plausible real {PASSWORD_VAR} "
        f"value - use an obvious placeholder; found: {bad}"
    )


@pytest.mark.parametrize(
    "path", [REFERENCE, ENV_EXAMPLE], ids=["REFERENCE.md", "env-example"]
)
def test_only_example_com_email_addresses(path):
    bad = _non_example_emails(path.read_text())
    assert not bad, (
        f"{path.name} must not contain real e-mail addresses - only "
        f"example.com placeholders; found: {bad}"
    )


@pytest.mark.parametrize(
    "path", [REFERENCE, ENV_EXAMPLE], ids=["REFERENCE.md", "env-example"]
)
def test_no_real_mail_provider_hosts(path):
    """Assignment values must use obvious placeholder hosts. Prose mentions
    of a provider name (e.g. 'a Gmail app-password') are fine."""
    bad = []
    for lineno, raw, _commented in _assignments(path.read_text(), HOST_VAR):
        if _MAIL_PROVIDER_RE.search(raw):
            bad.append((lineno, raw))
    assert not bad, (
        f"{path.name} must use obvious placeholders (smtp.example.com), not "
        f"a real mail provider host; found: {bad}"
    )


def test_host_value_is_obvious_placeholder():
    bad = []
    for lineno, raw, _commented in _assignments(_env_example_text(), HOST_VAR):
        low = raw.lower()
        if (
            raw
            and "example.com" not in low
            and not any(tok in low for tok in PLACEHOLDER_TOKENS)
        ):
            bad.append((lineno, raw))
    assert not bad, (
        f"{HOST_VAR} must be shown with an obvious placeholder such as "
        f"smtp.example.com; found: {bad}"
    )


# ---------------------------------------------------------------------------
# 8. Heading placement: the new ## section must not truncate the existing
#    'Per-role provider/model configuration' section (its body is read by
#    tests/unit/test_docs_provider_setup.py up to the NEXT '## ' heading).
# ---------------------------------------------------------------------------

def test_provider_config_section_body_not_truncated():
    body = _h2_section_body(
        _reference_text(), "Per-role provider/model configuration"
    )
    assert "role_config" in body, (
        "the 'Per-role provider/model configuration' section body lost its "
        "role_config content - a new '## ' heading was likely inserted "
        "inside it"
    )
    assert "PIPELINE_BACKEND_" in body, (
        "the 'Per-role provider/model configuration' section body lost its "
        "PIPELINE_BACKEND_ content - a new '## ' heading was likely "
        "inserted inside it"
    )
    assert "```json" in body, (
        "the 'Per-role provider/model configuration' section body lost its "
        "example registry JSON block - a new '## ' heading was likely "
        "inserted inside it"
    )


def test_email_env_var_table_rows_are_not_duplicated():
    """Reviewer round-2 regression guard: the e-mail env-var table in
    REFERENCE.md must list each PIPELINE_NOTIFY_EMAIL_* variable exactly
    once. A previous rework inserted the ENABLED/USER rows after the
    pre-existing HOST/PORT rows instead of replacing them, leaving
    byte-identical duplicate rows. Every markdown table row (a line starting
    with the '|' row marker) must contribute unique variable names."""
    text = (REPO_ROOT / "REFERENCE.md").read_text()
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.startswith("|"):
            continue
        names = re.findall(r"PIPELINE_NOTIFY_EMAIL_[A-Z0-9_]+", line)
        assert len(names) == len(set(names)), (
            f"REFERENCE.md:{lineno} duplicates an e-mail env var in one "
            f"table row: {names}"
        )
    all_row_names = [
        name
        for line in text.splitlines()
        if line.startswith("|")
        for name in re.findall(r"PIPELINE_NOTIFY_EMAIL_[A-Z0-9_]+", line)
    ]
    assert len(all_row_names) == len(set(all_row_names)), (
        "the REFERENCE.md e-mail env-var table lists a variable more than "
        f"once across rows: {sorted(all_row_names)}"
    )