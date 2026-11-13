# Audit-chain threat model and security review

This is the security review gate for the audit-anchor plan
(`store-stage-b4-audit-anchor`). ADR 0012 decision 10 makes the audit chain an
append-only record set with a pluggable `AnchorSink`; this page states what each
attacker class can and cannot do, so every later diff can be checked against it
rather than against the author's intent.

The document describes the behaviour already on `origin/master` plus the one
change the plan makes (reserving the plan name `host`). Anything here that a
story alters is a security regression until a human reviewer says otherwise.

## Assets

Four classes of record are in scope.

- Per-plan audit chains: `<plan>.audit.jsonl` in `PLAN_DIR` (default
  `~/.claude/plans`), written by `pipeline/audit.py`. One append-only JSON Lines
  stream per plan, each line carrying `seq`, `prev_hash`, `hash`, `ts`, `actor`,
  `correlation_id`, `action`, `outcome`, `target`, `detail` and the attribution
  fields. The hash covers the canonical JSON of every other field, so the stream
  is a hash chain and not merely a log.
- The host chain: `.audit.host.jsonl` (`HOST_AUDIT_FILE` in
  `pipeline/audit.py`), the same record shape for ticks that are not bound to a
  plan. In today's anchor log it is keyed `HOST_CHAIN` `"host"`
  (`pipeline/audit_anchor.py`, used by `_chain_streams` and `_stream_for`), the
  same string a plan named `host` would use; Known gaps (i) states that collision
  and how this plan closes it.
- The decisions chains: `<plan>.decisions.chain.jsonl`, the hash-chained record
  of review/approve/merge/refuse decisions defined by ADR 0014. They are held to
  the same append-only and hash-chain rules as the audit chains and are anchored
  through the same sink.
- The anchor log: the file named by `PIPELINE_AUDIT_ANCHOR=file:<absolute path>`.
  It is an append-only record of each chain's `(seq, hash)` head, living outside
  `PLAN_DIR`, that the pipeline host is not supposed to be able to rewrite. It
  is the only place a chain's high-water mark survives the chain itself.

## Attackers

| Attacker class | can append | can edit/delete/reorder a record | can truncate the tail | can rewrite a whole chain | what detects it |
| --- | --- | --- | --- | --- | --- |
| (a) App-level access: code running as the pipeline (a compromised agent, an MCP caller, a dashboard request) that reaches only the `AuditLog` surface | yes, only through the append path the surface exposes | no: the surface has no update, delete or reorder method | no: there is no truncate method, and the tail is not addressable | no: the surface cannot rewrite a record it already wrote | nothing new at the app boundary; the records it does append are well-formed and verify. A record it forges about itself is still evidence that it acted |
| (b) Host filesystem access to `PLAN_DIR` as the pipeline user | yes, by writing a line by hand | yes, in-place edits, deleted middle records and reordering all break the chain | yes, and `_verify_chain_at` alone cannot see it | yes, by re-serialising the whole file | `verify` reports the first bad record for edits, deletions, reorders, duplicates, gaps and malformed lines; tail truncation and a whole-chain rewrite are only visible against the anchor |
| (c) Database administrator of the SQLite store (later the Postgres store) | yes, `INSERT` | no through the API, but yes with direct `UPDATE`/`DELETE` — SQLite has no roles to stop a DBA | yes, with direct SQL | yes, with direct SQL | nothing at the database layer: SQLite offers no privilege model here; the append-only guarantee is an API and trigger property, and a DBA who edits rows is caught only if the same record is also anchored |
| (d) Operator or attacker with write access to the anchor sink | yes, can append anchor lines | yes, can edit or delete anchor lines | yes, can remove the newest anchor lines | yes, can rewrite the anchor log outright | nothing, if the sink is a plain file on the same host as `PLAN_DIR`. The whole point of the pluggable `AnchorSink` is that a WORM mount, an object-lock bucket or another host's append-only export removes this row from the model |

The rows are cumulative in power, not in likelihood. Row (a) is the attacker the
system is designed to survive; rows (b) and (c) are the reason the anchor exists;
row (d) is only out of scope once the sink is somewhere the pipeline host cannot
write.

## What the chain detects

`pipeline/audit.py _verify_chain_at` walks a chain from seq 0 and checks all
three links a tamper breaks: the running `seq`, the `prev_hash` carried from the
previous line, and each line's own `hash`, plus a JSON parse of the line. It
returns the 0-based record position of the first bad record — not the last good
one, not the end of the file — and `None` when the stream is intact.
`verify_audit_chain` surfaces that position to the caller and the `verify` tool
prints it; `REFERENCE.md` words the same value as "the index of the first
altered record". On a well-formed prefix the position and the record's own `seq`
are the same number, which is why the two are loosely called the same thing;
where a line is duplicated, missing or out of order, read the returned number as
the position of the first line that is out of step (a file `0,1,1,2` returns 2,
the position of the offending line, and `0,2` returns 1).

- Edited record: editing the payload at seq 1 in a chain `0,1,2` changes the
  record's `hash`, so the stored hash no longer matches the recomputed one and
  verify reports seq 1 as the first bad record.
- Deleted middle record: deleting seq 1 from `0,1,2` leaves `0,2`; seq 2's
  `prev_hash` no longer points at seq 0's hash, so the record with seq 2 is the
  first bad record.
- Reordered records: reordering a chain `0,1,2` so the file reads `0,2,1` breaks
  seq 2's `prev_hash` first, so the first bad record is seq 2.
- Duplicate seq: a file `0,1,1,2` repeats the running counter; the second seq 1
  is the first bad record, because its `seq` is not the expected next value.
- Seq gap: `0,2` skips seq 1, so seq 2 is the first bad record even if its hash
  recomputes cleanly.
- Malformed line: a line that is not valid JSON cannot carry a checkable hash, so
  the malformed position itself is reported as the first bad record.

The common mistake is to report only the last record, to skip to the end of the
file and judge it, or to treat a duplicate seq or a gap as valid because the
payload hashes still parse. Each of those lets an attacker who edited record 3 and
also re-hashed records 4 onwards pass a verifier that was supposed to catch the
edit at record 3. The first bad record is the whole answer; anything later is
already poisoned.

## What only the anchor detects

Two tampers are invisible to `_verify_chain_at` because they leave a shorter but
internally consistent chain:

- Tail truncation: dropping the newest records leaves `0,1` out of a chain that
  ran to 2. `0,1` verifies intact. Only the anchor log knows the chain once
  reached seq 2, so only anchor comparison can call this a truncation.
- Whole-chain rewrite: replacing every record with a freshly hashed fork from
  seq 0 produces a chain that verifies from end to end. Only the anchor log,
  which recorded the old heads, can tell the fork from the original.

A whole-chain rewrite that happens **before the first anchor pass** is
undetectable: the anchor log holds no head for that chain yet, so the first pass
will vouch for whatever head it finds. Everything the anchor buys is bought at
the first pass and kept from then on. This is a real gap, not a rounding error:
the anchor is only meaningful for chains that have been anchored.

Worked example, tracing the state. A chain `0,1,2` has been anchored with head
seq 2 / hash `H2`. Truncate the tail by deleting seq 2. Now `_verify_chain_at`
alone passes for `0,1` — the remaining two records are internally consistent —
but anchor comparison sees that the anchored head of 2 is missing from the
stream and reports tail truncation. If a later append then creates a new seq 2
with hash `H2'`, the existing anchor still expects seq 2 / hash `H2`, so the
truncation remains visible: the follow-up call after that append still reports
the mismatch, because the anchored hash at seq 2 has not changed. Anchors only
ratchet upwards, so the anchor head is never replaced by the forger's head.

The common mistake is to clamp the anchor high-water mark down to the current
observed head, or to compare only chain lengths. Either erases exactly the state
that makes the next comparison meaningful: once the forger's head becomes the
anchor, the truncation is laundered and every later pass sees a consistent
chain.

## Failure behaviour (preserved by this plan)

These are read from `origin/master`. The plan re-implements the store around
them and must not change one of them; a story that does is a security
regression, not a refactor.

1. `pipeline/audit.py record_audit` absorbs an append failure. When the append
   raises `OSError`, `record_audit` logs at ERROR and lets the caller's decision
   stand: it never turns an allowed action into a failure, and it never turns a
   denied action into an allowed one.

   Worked example: an allowed merge whose audit append raises `OSError` is still
   allowed, and the ERROR line records that the audit append was lost. A denied
   merge whose append raises `OSError` is still denied. The audit trail may miss
   a line; it must never change an outcome, because a storage outage would
   otherwise become an authorisation bypass in one direction or a denial of
   service in the other.

2. `PIPELINE_AUDIT_ANCHOR` configures the sink. Unset (or empty) means no
   anchoring at all: no anchor file is created and verification reports nothing
   about anchors. Any other value than `file:<absolute path outside PLAN_DIR>`
   raises `ValueError` at first use, naming the variable only and never the value
   it was given.

   Worked example: `PIPELINE_AUDIT_ANCHOR=file:/var/lib/audit/anchor.jsonl` is
   accepted. `PIPELINE_AUDIT_ANCHOR=/var/lib/audit/anchor.jsonl` (missing
   `file:`), a relative path, or a path inside `PLAN_DIR` fails closed the first
   time the sink is used, with a message naming `PIPELINE_AUDIT_ANCHOR` and
   nothing else.

3. An anchor log that exists but cannot be read raises `AnchorUnreadable`.
   Verification reports `anchor log unreadable: <OSError type>` and an anchoring
   pass appends nothing. A missing anchor log is an empty one; an unreadable
   anchor log is not.

   Worked example: the anchor holds head 5 / hash `H5` and becomes unreadable
   after a permission change. The old anchor stays intact, and verification
   reports `anchor log unreadable: PermissionError`. When the permissions are
   fixed, the chain is observed at head 3 / hash `H3`, which still reports tail
   truncation against the recorded 5 / `H5`. Reading the unreadable log as empty
   would have recorded 3 / `H3` fresh and permanently laundered the truncation.

4. The anchor file is opened with `O_NOFOLLOW` and its mode is re-imposed to
   `0o600` on the open descriptor. A refused open appends nothing.

   Worked example: a symlink is planted at the anchor path in a writable sink
   directory. `O_NOFOLLOW` refuses the open rather than following it, so nothing
   is written through the link and the previous anchors stay in force for later
   passes. A pre-existing anchor file with a looser mode is tightened to `0o600`
   without being truncated.

5. The scheduler sweep in `pipeline/watchers.py` wraps the `anchor_heads` call
   in a `try`/`except` that logs a WARNING carrying the exception type and
   continues the tick. Tamper evidence never stops dispatch.

   Worked example: `anchor_heads` raises `OSError` because the sink went away.
   `watchers.py` logs `audit anchor pass failed: OSError` and the sweep carries
   on to publish the snapshot and return its events. Dispatch is unaffected: a
   broken sink is an alarm, not a circuit breaker.

6. Anchors only ratchet upwards. A chain's anchor is the highest `seq` ever
   recorded for it, and at equal `seq` the hash recorded first. A later pass can
   raise the anchor but never lower it, and never replace a hash it already
   vouched for. A head that went backwards or changed hash is left un-anchored
   and keeps failing verification on every following tick.

## Known gaps

- (i) The anchor log format keys the host chain as `"host"` —
  `pipeline/audit_anchor.py HOST_CHAIN` and `_chain_streams` — which is the same
  string a plan named `host` would use for its own chain. Today that is an
  ambiguity a forger could exploit by renaming a plan. This plan closes it by
  reserving the plan name `host`: `save_plan`, `ingest_plan` and the record store
  refuse a plan named `host`. Inside the `AuditLog` the host chain is
  `HOST_CHAIN_ID` `"@host"`, which no plan name can spell, so the collision
  cannot reappear through a different entry point.

  The common mistake is to fix only one of the three entry points. A name
  reserved in `save_plan` but accepted by `ingest_plan` or the record store is
  not reserved at all.

- (ii) `pipeline/audit.py _tail` returns `None` for a malformed last line, so the
  next append restarts at seq 0. The malformed line is not skipped and the
  chain is not continued.

  Worked example: appends A → seq 0 and B → seq 1, then a malformed final line is
  written. `_tail` reads the last non-blank line, fails to parse it, and returns
  `None`, so the next append C starts a new chain at seq 0 rather than at seq 2.
  The file now reads seq 0 / A, seq 1 / B, the malformed line, then seq 0 / C. A
  follow-up verify still reports the earlier malformed position as the first bad
  record; it does not silently accept the restarted chain. The tail is damaged,
  not reset.

  The common mistake is to assume `_tail` skips the malformed line and the next
  append continues at seq 2 — which would be exactly the behaviour that hides the
  damage from a verifier that only looks at the tail.

- (iii) SQLite has no roles. The intended INSERT+SELECT-only shape is enforced at
  the API, by there being no update or delete method on the audit surface at all,
  and by triggers that refuse a `UPDATE` or `DELETE` on the record table — not by
  granting database privileges. A database administrator who connects directly
  can still change rows; the guarantee is API-level and trigger-level, and it
  becomes a privilege-level guarantee only when the store moves to Postgres and
  the pipeline connects as a role that holds INSERT and SELECT but not UPDATE or
  DELETE.

- (iv) A file anchor on the same host as `PLAN_DIR` raises the bar — a forger has
  to edit or delete anchor lines as well as chain lines — but such a file anchor
  is not tamper-proof. The same principal who can rewrite the chain can rewrite the
  anchor file. Only a sink the pipeline host cannot write (a WORM mount, an
  object-lock bucket, another host's append-only export) moves row (d) of the
  attacker table out of scope.

## Review checklist

Every story in plan `store-stage-b4-audit-anchor` is reviewed against these
questions before merge, on top of the usual review:

- Are the hash bytes unchanged? A record written by the old code must verify and
  must hash identically under the new code; a changed canonicalisation is a
  silent whole-chain rewrite for every existing chain.
- Does the audit surface still have no update, delete or reorder method? The
  append-only property is carried by the absence of those methods, not by
  discipline in the callers.
- Is every failure behaviour in this document preserved exactly? The
  `record_audit` `OSError` rule, the `PIPELINE_AUDIT_ANCHOR` `ValueError` rule, the
  `AnchorUnreadable` verdict text, the `O_NOFOLLOW`/`0o600` open, the `watchers.py`
  WARNING-and-continue sweep, and the upward-only ratchet.
- Does any error, log line or exception echo a path, an anchor value or a secret?
  An error names the variable, never its value; a verdict names the `OSError`
  type, never the file it could not open. The same rule applies to the WARNING in
  the sweep.
