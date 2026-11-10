"""Pinning test for the audit-chain threat model (docs/AUDIT_CHAIN_THREAT_MODEL.md).

The threat model is the security review gate for the audit-anchor plan: it is
the one page a reviewer checks every later diff against. These tests pin the
structure of that page - the seven level-2 sections, the attacker table, the six
preserved failure behaviours, the four known gaps and the review checklist -
plus the one link into it from REFERENCE.md's "### Audit stream" section.

The Markdown is read as text and never parsed: a heading must be present
verbatim, and a section must name the artefacts, code paths and failure modes it
is about. Prose is not pinned word for word.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs/AUDIT_CHAIN_THREAT_MODEL.md"
REFERENCE = ROOT / "REFERENCE.md"

#: The level-2 sections the threat model must carry.
HEADINGS = (
    "Assets",
    "Attackers",
    "What the chain detects",
    "What only the anchor detects",
    "Failure behaviour (preserved by this plan)",
    "Known gaps",
    "Review checklist",
)

#: The one sentence REFERENCE.md gains in its "### Audit stream" section.
LINK_SENTENCE = "The threat model is in docs/AUDIT_CHAIN_THREAT_MODEL.md."

#: A new list item starts at a numbered, bulleted or "(i)"-style line.
ITEM_START = re.compile(r"^\s*(?:\d+[.)]|[-*+]|\((?:[ivx]+|\d+)\))\s+")


def doc_text() -> str:
    """Return the threat model's text, failing when the document is missing."""
    assert DOC.is_file(), f"missing {DOC}"
    return DOC.read_text(encoding="utf-8")


def sections(text: str) -> dict[str, str]:
    """Map every level-2 heading to the body that follows it."""
    out: dict[str, str] = {}
    name: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if name is not None:
                out[name] = "\n".join(body)
            name = line[3:].strip()
            body = []
        elif name is not None:
            body.append(line)
    if name is not None:
        out[name] = "\n".join(body)
    return out


def section(name: str) -> str:
    """Return the body of one level-2 section."""
    found = sections(doc_text())
    assert name in found, f"the threat model has no '## {name}' section"
    return found[name]


def flat(text: str) -> str:
    """Collapse whitespace so a phrase is found even when the Markdown wraps it."""
    return re.sub(r"\s+", " ", text)


def mentions(body: str, *needles: str) -> bool:
    """True when *body* holds at least one of *needles* (case-insensitive)."""
    low = flat(body).lower()
    return any(flat(needle).lower() in low for needle in needles)


def norm(text: str) -> str:
    """Fold spacing and case so ``AuditLog``, ``audit log`` and ``audit_log`` match."""
    return re.sub(r"[\s_\-=`]+", "", text).lower()


def items(body: str) -> list[str]:
    """Split a section body into its list items (or paragraphs, if unlisted)."""
    out: list[str] = []
    current: list[str] = []
    for line in body.splitlines():
        if ITEM_START.match(line) and current:
            out.append("\n".join(current))
            current = []
        if line.strip():
            current.append(line)
    if current:
        out.append("\n".join(current))
    return out


def scoped(body: str, *keywords: str) -> str:
    """Return the items of *body* that mention any of *keywords*."""
    picked = [p for p in items(body) if any(k.lower() in p.lower() for k in keywords)]
    assert picked, f"the section has no item mentioning any of {keywords}"
    return "\n".join(picked)


def is_separator(cells: list[str]) -> bool:
    """True for a Markdown table separator row (``|---|---|``)."""
    return bool(cells) and all("-" in cell and set(cell) <= set("-: ") for cell in cells)


def table_blocks(body: str) -> list[list[list[str]]]:
    """Return each Markdown table in *body* as a block of cell lists."""
    blocks: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            current.append([cell.strip() for cell in stripped.strip("|").split("|")])
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def attacker_table(body: str) -> list[list[str]]:
    """Return the attacker table: the first table whose header names the columns."""
    for block in table_blocks(body):
        header = " ".join(block[0]).lower()
        if all(
            needle in header
            for needle in ("append", "edit", "delete", "reorder", "truncat", "rewrit")
        ):
            return block
    return []


def attacker_rows(body: str) -> list[str]:
    """Return the attacker table's data rows, one string per row."""
    block = attacker_table(body)
    assert block, "the Attackers section has no table with the capability columns"
    return [" ".join(row) for row in block[1:] if not is_separator(row)]


# --- the document and its headings ------------------------------------------------


def test_threat_model_document_exists_and_is_not_empty():
    """docs/AUDIT_CHAIN_THREAT_MODEL.md exists, is not empty and has the seven sections."""
    assert DOC.is_file(), f"missing {DOC}"
    text = doc_text()
    assert text.strip(), "the threat model is empty"
    lines = text.splitlines()
    found = sections(text)
    for heading in HEADINGS:
        assert f"## {heading}" in lines, f"the threat model has no '## {heading}' heading"
        assert heading in found, f"'## {heading}' is not a level-2 section"
        assert found[heading].strip(), f"the '## {heading}' section is empty"


# --- assets ------------------------------------------------------------------------


def test_assets_section_names_every_audited_artefact():
    """Assets names the plan chains, the host chain, the decisions chains and the sink."""
    body = section("Assets")
    assert "audit.jsonl" in body, "the Assets section does not name the per-plan chain"
    assert ".audit.host.jsonl" in body, "the Assets section does not name the host chain"
    assert "decisions.chain.jsonl" in body, (
        "the Assets section does not name the decisions chain"
    )
    assert mentions(body, "0014"), "the Assets section does not cite ADR 0014"
    assert "PIPELINE_AUDIT_ANCHOR" in body, (
        "the Assets section does not name the sink variable"
    )
    assert "file:" in body, "the Assets section does not give the sink's file: form"
    assert mentions(body, "absolute"), (
        "the Assets section does not say the sink path is absolute"
    )


# --- attackers ---------------------------------------------------------------------


def test_attackers_section_is_a_table_with_the_capability_columns():
    """The attacker table carries every capability column the review needs."""
    block = attacker_table(section("Attackers"))
    assert block, "the Attackers section has no table with the capability columns"
    header = " ".join(block[0]).lower()
    for needle in ("append", "edit", "delete", "reorder", "truncat", "rewrit", "detect"):
        assert needle in header, f"the attacker table has no '{needle}' column"
    rows = [row for row in block[1:] if not is_separator(row)]
    assert len(rows) >= 4, "the attacker table needs one row per attacker class"
    for row in rows:
        assert len(row) == len(block[0]), "the attacker table has a ragged row"


def test_attacker_table_has_one_row_per_attacker_class():
    """One row each for app-level access, PLAN_DIR, the DBA and the anchor writer."""
    rows = attacker_rows(section("Attackers"))
    folded = [norm(row) for row in rows]

    def indices(predicate) -> set[int]:
        return {i for i, row in enumerate(folded) if predicate(row)}

    app = indices(
        lambda row: "auditlog" in row
        and any(word in row for word in ("agent", "mcp", "dashboard", "inprocess", "coderunning"))
    )
    assert app, (
        "no row for code running as the pipeline (an agent, an MCP caller, a dashboard request)"
    )
    assert any(
        mentions(rows[i], "only", "surface") for i in app
    ), "the app-level row does not say the attacker reaches only the AuditLog surface"

    filesystem = indices(lambda row: "plandir" in row)
    assert filesystem, "no row for host filesystem access to PLAN_DIR"

    database = indices(
        lambda row: "sqlite" in row
        and any(word in row for word in ("administrator", "database", "dba"))
    )
    assert database, "no row for the database administrator of the SQLite store"
    assert any(
        mentions(row, "postgres") for row in rows
    ), "no mention of the later Postgres store"

    sink = indices(
        lambda row: "anchor" in row
        and any(word in row for word in ("sink", "operator", "write"))
    )
    assert sink, "no row for an operator or attacker with write access to the anchor sink"

    covered = app | filesystem | database | sink
    assert len(covered) >= 4, (
        "the attacker table needs four distinct rows: one per attacker class"
    )


# --- what the chain detects ---------------------------------------------------------


def test_chain_detection_section_lists_every_detected_tamper():
    """The chain section names each tamper the chain itself catches."""
    body = section("What the chain detects")
    low = body.lower()
    for needle in ("edit", "delet", "reorder", "duplicate", "gap", "malformed"):
        assert needle in low, f"the chain-detects section does not mention {needle}"
    assert "seq" in low, "the chain-detects section does not report a seq"
    assert "first" in low, "the chain-detects section does not report the first bad record"
    assert mentions(body, "verify"), "the chain-detects section does not name the verifier"
    assert "_verify_chain_at" in body, (
        "the chain-detects section does not name _verify_chain_at"
    )
    assert "audit.py" in body, "the chain-detects section does not name pipeline/audit.py"


def test_anchor_only_section_scopes_truncation_and_whole_chain_rewrite():
    """The anchor-only section names tail truncation and a whole-chain rewrite."""
    body = section("What only the anchor detects")
    low = body.lower()
    assert "truncat" in low, "the anchor-only section does not name tail truncation"
    assert "rewrit" in low, "the anchor-only section does not name a whole-chain rewrite"
    assert "anchor" in low
    assert mentions(
        body,
        "undetectable",
        "cannot be detected",
        "not detectable",
        "indistinguishable",
    ), "the anchor-only section does not say a pre-anchor rewrite is undetectable"
    assert mentions(
        body, "before the first", "before any anchor", "first anchor", "before the chain"
    ), "the anchor-only section does not scope the gap to a rewrite before the first anchor"


# --- failure behaviour --------------------------------------------------------------


def test_failure_behaviour_keeps_the_append_failure_semantics():
    """record_audit still logs at ERROR and never changes the caller's decision."""
    item = scoped(section("Failure behaviour (preserved by this plan)"), "record_audit")
    assert "record_audit" in item, "the failure section does not name record_audit"
    assert "ERROR" in item, "the append-failure item does not say it logs at ERROR"
    assert "OSError" in item, "the append-failure item does not name the OSError it absorbs"
    assert mentions(item, "allowed", "permit"), (
        "the append-failure item does not speak of an allowed action"
    )
    assert mentions(item, "denied", "refus"), (
        "the append-failure item does not speak of a denied action"
    )
    assert mentions(item, "stand", "unchanged", "does not change"), (
        "the append-failure item does not say the caller's decision stands"
    )


def test_failure_behaviour_keeps_the_anchor_configuration_rules():
    """An unset sink means no anchoring; any other value fails closed at first use."""
    item = scoped(
        section("Failure behaviour (preserved by this plan)"), "PIPELINE_AUDIT_ANCHOR"
    )
    assert mentions(item, "unset", "not set", "empty", "no anchoring"), (
        "the sink-configuration item does not say an unset variable means no anchoring"
    )
    assert "ValueError" in item, "the sink-configuration item does not name the ValueError"
    assert "file:" in item, "the sink-configuration item does not give the only accepted form"
    assert mentions(item, "absolute"), (
        "the sink-configuration item does not require an absolute path"
    )
    assert "PLAN_DIR" in item, (
        "the sink-configuration item does not require a path outside PLAN_DIR"
    )
    assert mentions(item, "variable"), (
        "the sink-configuration item does not say the message names the variable"
    )
    assert mentions(item, "first use", "firstuse", "first time"), (
        "the sink-configuration item does not say the ValueError comes at first use"
    )


def test_failure_behaviour_keeps_the_unreadable_anchor_rule():
    """An existing but unreadable anchor log raises AnchorUnreadable and anchors nothing."""
    body = section("Failure behaviour (preserved by this plan)")
    assert "AnchorUnreadable" in body, "the failure section does not name AnchorUnreadable"
    item = scoped(body, "AnchorUnreadable")
    assert mentions(item, "anchor log unreadable"), (
        "the unreadable-anchor item does not give the verdict text"
    )
    assert "OSError" in item, (
        "the unreadable-anchor item does not say the verdict names the OSError type"
    )
    assert mentions(
        item, "appends nothing", "nothing is appended", "writes nothing", "anchors nothing"
    ), "the unreadable-anchor item does not say the anchoring pass appends nothing"


def test_failure_behaviour_keeps_the_sink_file_opening_rules():
    """The sink file is opened O_NOFOLLOW, re-chmodded 0o600, and a refusal anchors nothing."""
    body = section("Failure behaviour (preserved by this plan)")
    item = scoped(body, "O_NOFOLLOW")
    assert "O_NOFOLLOW" in item, "the sink-file item does not name O_NOFOLLOW"
    assert "0o600" in item, "the sink-file item does not name the 0o600 mode"
    assert mentions(item, "refus"), (
        "the sink-file item does not say a refused open is refused"
    )
    assert mentions(
        item, "appends nothing", "nothing is appended", "writes nothing", "anchors nothing"
    ), "the sink-file item does not say a refused open appends nothing"


def test_failure_behaviour_keeps_the_sweep_continuing():
    """The sweep logs a WARNING and continues: tamper evidence never stops dispatch."""
    body = section("Failure behaviour (preserved by this plan)")
    item = scoped(body, "watchers.py")
    assert "anchor_heads" in item, "the sweep item does not name the anchor_heads call"
    assert "WARNING" in item, "the sweep item does not say the sweep logs at WARNING"
    assert mentions(
        item, "exception type", "error type", "type name", "type of the exception"
    ), "the sweep item does not say what the warning carries"
    assert mentions(item, "continues", "carries on", "does not stop")
    assert mentions(item, "dispatch"), "the sweep item does not say dispatch continues"


def test_failure_behaviour_keeps_anchors_ratcheting_upwards():
    """An anchor is only ever raised, never lowered."""
    body = section("Failure behaviour (preserved by this plan)")
    item = scoped(body, "ratchet")
    assert mentions(
        item, "upward", "upwards", "never lower", "never lowered", "only ever raises"
    ), "the ratchet item does not say the anchor only ratchets upwards"


# --- known gaps ---------------------------------------------------------------------


def test_known_gaps_names_the_host_chain_name_collision():
    """The 'host' chain key collision and the reservation of the plan name 'host'."""
    item = scoped(section("Known gaps"), "HOST_CHAIN")
    assert "_chain_streams" in item, "the host-collision gap does not name _chain_streams"
    assert "audit_anchor.py" in item, (
        "the host-collision gap does not name pipeline/audit_anchor.py"
    )
    assert "save_plan" in item, "the host-collision gap does not name save_plan"
    assert mentions(item, "ingest"), "the host-collision gap does not name the ingest path"
    assert mentions(item, "record store"), (
        "the host-collision gap does not name the record store"
    )
    assert mentions(item, "reserv"), (
        "the host-collision gap does not say the plan name is reserved"
    )
    assert "HOST_CHAIN_ID" in item, "the host-collision gap does not name HOST_CHAIN_ID"
    assert "@host" in item, "the host-collision gap does not spell the host chain id"


def test_known_gaps_names_the_malformed_tail_gap():
    """_tail returns None for a malformed last line, so the next append restarts at seq 0."""
    item = scoped(section("Known gaps"), "_tail")
    assert "audit.py" in item, "the malformed-tail gap does not name pipeline/audit.py"
    assert mentions(item, "malformed"), (
        "the malformed-tail gap does not mention a malformed line"
    )
    assert mentions(item, "none", "null"), (
        "the malformed-tail gap does not say _tail returns None for a malformed last line"
    )
    assert mentions(
        item, "seq 0", "seq=0", "sequence 0", "seq zero", "restarts at 0", "restart at 0"
    ), "the malformed-tail gap does not say the next append restarts at seq 0"
    assert mentions(item, "verify"), (
        "the malformed-tail gap does not say verify still reports the position"
    )


def test_known_gaps_names_the_sqlite_role_gap():
    """SQLite has no roles: INSERT+SELECT-only is enforced at the API and by triggers."""
    item = scoped(section("Known gaps"), "SQLite")
    assert "INSERT" in item, "the sqlite-role gap does not name the INSERT privilege"
    assert "SELECT" in item, "the sqlite-role gap does not name the SELECT privilege"
    assert mentions(item, "trigger"), "the sqlite-role gap does not name the triggers"
    assert mentions(item, "privileg"), (
        "the sqlite-role gap does not speak of database privileges"
    )
    assert re.search(
        r"\b(?:no|not|without)\b[^.]*?\brole", flat(item), re.IGNORECASE
    ), "the sqlite-role gap does not say SQLite has no roles"
    assert mentions(item, "update"), (
        "the sqlite-role gap does not name the missing update method"
    )
    assert mentions(item, "delete"), (
        "the sqlite-role gap does not name the missing delete method"
    )


def test_known_gaps_says_the_file_anchor_is_not_tamper_proof():
    """A file anchor on the same host raises the bar but is not tamper-proof."""
    item = scoped(section("Known gaps"), "tamper-proof")
    assert mentions(item, "is not tamper-proof"), (
        "the known gaps do not say the file anchor is not tamper-proof"
    )
    assert mentions(item, "same host", "same machine"), (
        "the file-anchor gap does not scope it to the same host"
    )
    assert mentions(item, "raise") and "bar" in item.lower(), (
        "the file-anchor gap does not say the file anchor raises the bar"
    )


# --- review checklist and the tamper-proof claim -------------------------------------


def test_review_checklist_covers_the_reviewer_questions():
    """The checklist asks the four questions every story of the plan must answer."""
    body = section("Review checklist")
    assert "store-stage-b4-audit-anchor" in body, "the checklist does not name the plan"
    low = body.lower()
    for needle in ("hash", "update", "delete", "error", "log", "path", "value", "secret"):
        assert needle in low, f"the review checklist does not ask about {needle}"
    assert mentions(body, "surface"), (
        "the review checklist does not ask about the audit surface"
    )


def test_tamper_proof_is_only_ever_claimed_as_a_negation():
    """'tamper-proof' is never a positive claim about the file anchor."""
    text = doc_text()
    assert "is not tamper-proof" in text, (
        "the threat model never denies the file anchor is tamper-proof"
    )
    assert "is tamper-proof" not in text, "the threat model claims the file anchor is tamper-proof"
    for match in re.finditer(r"tamper-proof", text, re.IGNORECASE):
        before = text[max(0, match.start() - 60) : match.start()].lower()
        assert re.search(r"\bnot\b|n't", before), (
            "'tamper-proof' is used as a positive claim about the file anchor"
        )


# --- the link from REFERENCE.md ------------------------------------------------------


def test_reference_audit_stream_section_links_the_threat_model():
    """REFERENCE.md's '### Audit stream' section links the threat model once."""
    lines = REFERENCE.read_text(encoding="utf-8").splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith("### Audit stream")),
        None,
    )
    assert start is not None, "REFERENCE.md has no '### Audit stream' section"
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("#"):
            break
        body.append(line)
    section_text = "\n".join(body)
    assert LINK_SENTENCE in section_text, (
        "REFERENCE.md's '### Audit stream' section does not link the threat model"
    )
    assert section_text.count(LINK_SENTENCE) == 1, (
        "REFERENCE.md's '### Audit stream' section links the threat model more than once"
    )
