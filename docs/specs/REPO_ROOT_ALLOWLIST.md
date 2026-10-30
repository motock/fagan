# Plan `repo_root` allowlist (`PIPELINE_ALLOWED_REPO_ROOTS`)

Status: implemented (ENT-PREP-01)
Code: `pipeline/workspace.py` (`ALLOWED_REPO_ROOTS_ENV`,
`check_repo_root_allowed`), `pipeline/service.py` (`PipelineService.save_plan`)

## What it is

`PIPELINE_ALLOWED_REPO_ROOTS` is an **opt-in** allowlist of directories that a
plan's top-level `repo_root` may live in. It exists so an operator can bound
where the pipeline will operate without changing any default behaviour.

The variable is read from the process environment **on every check**, so
re-pointing it takes effect immediately (no restart, no import-time caching).

## Delimiter

Entries are separated by `os.pathsep` — `:` on POSIX, `;` on Windows. Entries
are not trimmed: whitespace is significant, and a whitespace-only value is a
malformed entry (see fail-closed rules).

## Unset vs. set semantics

| Value | Meaning |
| --- | --- |
| variable unset | **No restriction.** Every candidate is allowed; behaviour is byte-for-byte identical to before this feature. |
| exactly `""` (empty string) | **No restriction.** Same as unset. |
| any other value | **Allowlist.** A candidate is allowed only when it equals an entry or is a descendant of one. |

A candidate is allowed when, after `expanduser().resolve()` on both sides, it
equals an entry or the entry is one of the candidate's parent path components.
Component-wise comparison means `/srv/repos-evil/project` is **not** a
descendant of `/srv/repos`.

## Fail-closed rules

Any of the following **denies** the candidate:

- an **empty entry** (e.g. `":"` on POSIX, or a trailing separator) — the
  variable is malformed, not "unset";
- a **relative entry** (e.g. `repos`, `relative/repo`) — it is never silently
  anchored to the process working directory;
- an **unresolvable entry** (e.g. `~nosuchuser/repo`) — expansion failure is a
  denial, surfaced as a `ValueError`, never a `RuntimeError`;
- a **whitespace-only value** (e.g. `"   "`) — it is not the empty string, so
  it is a malformed entry;
- a candidate that is outside every entry.

An invalid entry denies the **whole** check, even when another entry in the
same value would have matched. There is no partial trust.

The raised error is a `ValueError` (specifically
`pipeline.workspace.WorkspaceSecurityError`, a `ValueError` subclass). Its
message names `PIPELINE_ALLOWED_REPO_ROOTS` and never echoes the candidate path
or an entry, so it is safe to surface to a caller.

## Example

Allow plans under two home-relative trees:

```sh
export PIPELINE_ALLOWED_REPO_ROOTS=~/repos:~/work/repos
```

- `~/repos/project` → allowed (descendant of `~/repos`)
- `~/repos` → allowed (exact match)
- `~/elsewhere/project` → denied

A relative entry is a misconfiguration and denies everything:

```sh
export PIPELINE_ALLOWED_REPO_ROOTS=relative/repo
```

## Enforced boundary

`PipelineService.save_plan` is the **enforced boundary**. On the
`workspace is None` path — the only MCP path that creates a plan source — a
string `repo_root` is validated before any filesystem write, and a denial
returns `{"ok": False, "error": ...}` having written nothing (no partial plan
file, no created parent directories).

When a `workspace` argument is supplied, the pre-existing WS-11 behaviour is
unchanged: `validate_workspace` validates it and the server-resolved path
overwrites the plan's `repo_root`. The allowlist is not consulted on that path.

`ingest_plan` is **not** enforced in this story. It does not create a plan
source, so the allowlist is deliberately not applied there yet.
