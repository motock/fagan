# Remote Execution (SSH)

## 1. Status

Remote/SSH execution is **implemented** (B1): one story's dispatch or review
harness can run on a remote GPU box instead of the orchestrator host. This is
a proof that remote dispatch works for a single story — it is not a remote
queue or a multi-host scheduler (see [§6](#6-scope-boundary-one-remote-story-not-a-remote-queue)).

## 2. Architecture

The orchestrator (the MCP server, the scheduler, `pipeline/story_status.py`'s
liveness polling) always stays on the local host. When a role's execution
mode resolves to `ssh`, `pipeline/execution.py`'s `spawn_harness` hands off to
`_spawn_ssh`, which launches a **local** supervisor subprocess
(`python -m pipeline.remote_exec`) and returns its pid immediately — that pid
is what gets written into `story["pid"]` on the manifest, so
`pipeline/story_status.py`'s liveness checks (`os.kill(pid, 0)`, the `ps`
zombie check, the dispatch watchdog, and the "agent produced no output"
zero-bytes-after-exit failed-launch detection keyed on `agent.log`) all keep
working completely unchanged — they are watching the local supervisor, not
the remote harness. The supervisor (`pipeline/remote_exec.py`) does the
actual off-box work in three steps: it calls
`pipeline/remote_sync.py`'s `ensure_remote_worktree` to force-push the local
story worktree's current tip into a bare git mirror on the remote host and
materialize (or reset) a remote worktree checked out at that tip; it then
runs the harness argv on the remote host over a single `ssh -o
BatchMode=yes` invocation, streaming the harness's own stdout/stderr straight
into the same `agent.log` the local path would have used (the supervisor
never opens or truncates a log of its own); and once the harness exits, it
calls `sync_back_commits` to fetch the remote tip and fast-forward the local
story worktree onto it, so any commits the remote harness made land back on
the branch the reviewer and merge gate actually operate on.

## 3. Configuration

All variables below fail closed: a required value that is missing or empty
raises a `ValueError` naming the variable, never a silent fallback to local
execution.

### 3.1 `PIPELINE_EXEC_DISPATCH` (and `PIPELINE_EXEC_<ROLE>` generally)

`resolve_execution_mode(role)` in `pipeline/execution.py` reads
`PIPELINE_EXEC_{ROLE.upper()}` — for the dispatch role that is
`PIPELINE_EXEC_DISPATCH`; the review role reads `PIPELINE_EXEC_REVIEW`.

- Unset or empty → defaults to `"local"`.
- `"local"` or `"ssh"` (case-insensitive) → used as given.
- Anything else → `ValueError` naming the variable, the offending value, and
  the valid choices, raised **before** any process is spawned or log file is
  created.

`PIPELINE_EXEC_DISPATCH=ssh` routes the dispatch-role harness through
`_spawn_ssh`; leaving `PIPELINE_EXEC_DISPATCH` unset (or `local`) keeps
dispatch on the orchestrator host exactly as before this feature existed.

### 3.2 `PIPELINE_REMOTE_EXEC_HOST`

Required whenever any role resolves to `ssh` mode. The ssh host `_spawn_ssh`
and the supervisor connect to (e.g. `gpu-box.example.com` or a `user@host`
form, since it is passed straight to `ssh`). Missing or empty when `ssh` mode
is selected raises `ValueError: PIPELINE_REMOTE_EXEC_HOST and
PIPELINE_REMOTE_SYNC_ROOT must be set` from `_spawn_ssh` in
`pipeline/execution.py`.

### 3.3 `PIPELINE_REMOTE_SYNC_ROOT`

Required whenever any role resolves to `ssh` mode. An absolute path on the
remote host under which the bare mirror and per-story worktrees live:

- the bare transport mirror lives at `<PIPELINE_REMOTE_SYNC_ROOT>/repo-bare.git`
  (composed into the URL `ssh://<PIPELINE_REMOTE_EXEC_HOST><PIPELINE_REMOTE_SYNC_ROOT>/repo-bare.git`);
- the remote worktree for a given story lives at
  `<PIPELINE_REMOTE_SYNC_ROOT>/worktrees/<story-worktree-dir-name>`, where the
  directory name is the local story worktree's own basename.

Missing or empty when `ssh` mode is selected raises the same `ValueError` as
§3.2 (both variables are checked together in `_spawn_ssh`).

### 3.4 Setting these variables

This repo's scheduler and MCP server read process environment, not a config
file, so set these the same way any other `PIPELINE_*` variable is set here:
`launchctl setenv PIPELINE_EXEC_DISPATCH ssh` (and the host/sync-root
variables) for an interactively-running Claude Code session, or add them to
the `EnvironmentVariables` block of the relevant `launchd/*.plist` job for the
scheduler daemon so they're picked up on every launchd-managed run. A
variable set only via `launchctl setenv` in one session does not propagate to
a separately-loaded launchd job — set it in both places if both need remote
execution.

## 4. GPU-box prerequisites

Checklist for the remote host before pointing any role at it with
`PIPELINE_EXEC_<ROLE>=ssh`:

- **SSH key auth, no prompts.** The supervisor connects with
  `ssh -o BatchMode=yes <host> ...` (`pipeline/remote_sync.py`'s `_ssh_run`
  and `pipeline/remote_exec.py`'s harness invocation both use it) — `BatchMode=yes`
  disables all interactive prompts (password, passphrase, host-key
  confirmation), so key-based auth must already be configured and the
  remote host's key must already be trusted (accept its host key once,
  out of band, before the first dispatch) or every connection attempt fails
  immediately.
- **`git` installed** on the remote host — `ensure_remote_worktree` runs
  `git init --bare`, `git worktree add`, and `git reset --hard` over the ssh
  connection.
- **A `.venv` provisioned at the same relative path inside the remote
  worktree**, for the `local` (Ollama/LM Studio/MLX) backend. That backend's
  dispatch `cmd[0]` is the local worktree's venv python
  (e.g. `<worktree>/.venv/bin/python`); `_spawn_ssh`'s path remapping in
  `pipeline/execution.py` (`_remap_path_prefix` / `_build_spec`) rewrites any
  argv element or env value that starts with the local worktree's absolute
  path onto the equivalent remote worktree path, but it does not provision
  the venv itself — the remote worktree needs its own working `.venv` at that
  path before the first ssh dispatch for that story. One-time setup once the
  bare mirror exists on the GPU box:

  ```bash
  cd <PIPELINE_REMOTE_SYNC_ROOT>/worktrees/<story-worktree-dir-name>
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  ```

- **`claude` CLI installed and authenticated**, for the `claude` backend —
  the harness argv for that backend shells out to the `claude` binary, so it
  must be on the remote host's `PATH` and already logged in (an interactive
  `claude` session's auth, not something the dispatch spec provisions).
- **`LOCAL_AGENT_ENDPOINT` reachable from the GPU box**, for the
  `ollama`/`lmstudio`/`mlx` local backend — the endpoint URL is carried in the
  dispatch env and remapped like any other value, but it is not rewritten to
  a remote address, so if the endpoint is `http://localhost:11434` on the
  orchestrator host, the remote GPU box needs its own reachable endpoint at
  that same URL (e.g. its own local Ollama instance, or a tunnel), not a
  loopback address that only resolves on the orchestrator.

## 5. Operational notes and failure modes

| Condition | Observed behavior | What to do |
|---|---|---|
| Remote host unreachable | `ssh -o BatchMode=yes` fails; `subprocess.run(..., check=True)` raises `CalledProcessError`, which propagates out of the supervisor uncaught — the supervisor process exits nonzero and the local liveness check (§2) reports the story as a failed/interrupted launch, same as any other crashed dispatch. | Verify connectivity and host-key trust (§4) before redispatching; the story is dispatch-eligible again like any other failed launch. |
| Remote harness command fails | The harness's own exit code is the supervisor's exit code (`pipeline/remote_exec.py`'s `main` returns `proc.wait()` as-is, unless sync-back also failed — see next row). | Read `agent.log` (streamed live from the remote harness) exactly as for a local run; the failure is graded the same way. |
| Sync-back divergence | `sync_back_commits` raises `RuntimeError` naming both the local and remote SHAs when neither is an ancestor of the other. The supervisor catches only `RuntimeError`/`SubprocessError` around the sync-back call, prints `sync-back failed for <branch>: <exc>` to stderr, and — if the harness itself exited 0 — reports exit code `1` so a sync-back failure is never silently treated as a success. | Investigate why the **local** story worktree has commits the remote branch doesn't already have (e.g. something committed into it while the remote run was in flight) — the local branch is never forced or reset to make this go away. Do not force-push or hard-reset either side to "fix" the divergence; resolve it by hand (merge or rebase) and redispatch. |
| Remote tip behind local tip | `sync_back_commits` raises a `RuntimeError` with a distinct message ("remote tip ... is behind local tip ... the remote contributed nothing") — this is not a merge, just a no-op refusal since the remote added nothing worth fast-forwarding to. | Usually means the remote harness made no commits; check `agent.log` for why before redispatching. |
| Re-dispatch of the same story | `ensure_remote_worktree` force-pushes the local tip into the bare mirror again, then resets the existing remote worktree to that freshly pushed tip (`git worktree add` fails because the path is already registered; the `||` fallback runs `git reset --hard` instead). Any uncommitted remote-side state from a prior run is discarded. | Expected and safe — the bare mirror is scratch, not history of record (see below). |

**The remote bare repo is a transport mirror, not history of record.** Every
dispatch force-pushes the local story worktree's current tip over whatever
the mirror previously held; the mirror's only job is to get commits onto the
remote worktree and back. History of record stays on the **local** story
worktree — `sync_back_commits` only ever fast-forwards it, never forces or
resets it, which is why divergence raises loudly instead of silently
discarding either side's work.

## 6. Scope boundary: one remote story, not a remote queue

This slice proves remote dispatch works for a single story on a single GPU
box. It deliberately does **not** include a remote queue, multi-host
scheduling, or any load-balancing across remote hosts — `PIPELINE_REMOTE_EXEC_HOST`
and `PIPELINE_REMOTE_SYNC_ROOT` name exactly one remote target for the whole
process. Dispatching many stories concurrently still means many independent
ssh sessions against that same one host.

**Review execution stays local in this slice.** `PIPELINE_EXEC_REVIEW=ssh` is
supported by the same `resolve_execution_mode`/`spawn_harness` machinery as
dispatch (both are just role names), but the deliberate design decision here
is that the reviewer, when run locally (the default), reads the
already-synced-back local story worktree — after `sync_back_commits` lands
the remote harness's commits onto the local branch, review has no remaining
dependency on the remote host. Running review itself on the remote host is a
possible follow-up, not part of this slice's scope.
