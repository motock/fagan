# Security audit — fagan harness, 2026-10-19

Scope: the harness's credential handling, agent process environment, plan
data as a system boundary, shell-outs, the dashboard/chat HTTP surface, and
secret hygiene in the repository. Audited at `1c790ce`.

Method: source review of the files named below plus targeted greps for
`shell=True`, `os.system`/`eval`/`exec`, hardcoded credentials, and
secret-bearing paths; a `git log -S` check that no known secret was ever
committed; and permission checks on the local secret files.

## Findings

### Medium — acceptance fixture paths are not validated before materialization

`pipeline/dispatch.py:508-513` writes every declared acceptance fixture with

```python
target = worktree_path / entry["path"]
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(entry["source"])
```

`entry["path"]` comes straight from plan data. An absolute path (`/tmp/x`) or
a `..` component (`../../../../tmp/x`) resolves outside the worktree, so a
malformed or hostile plan makes the harness create directories and write
files anywhere the user can. `pipeline/build_detect.py:250-254` already
applies the correct guard (`candidate.is_absolute() or ".." in
candidate.parts: continue`) for the same data; `pipeline/ingest.py`
validates the story `files` list but not `acceptance`.

Impact: arbitrary file write as the operator. Requires a plan that carries
the entry (operator- or decomposer-authored), so it is a trust-boundary
defect rather than a remote exploit.

Fix: apply the `build_detect` guard in `dispatch.py` (skip or reject
absolute / `..` paths), and optionally reject them at ingest so the story
fails loudly instead of silently dropping its oracle.

### Medium — model-driven agents inherit pipeline secrets

`pipeline/__init__.py` loads `.pipeline.env` into `os.environ` at import.
Agent launches pass the environment through unchanged:
`pipeline/execution.py::spawn_local` / `spawn_harness` (`env=` forwarded to
`Popen`) and `app/backend_claude.py:257` (`env={**os.environ,
**command.env}`). Every dispatched agent therefore carries
`PIPELINE_NOTIFY_EMAIL_PASSWORD` (the Gmail app password), and
MCP-server-spawned dispatches also carry `PLANE_API_KEY`.

Agents run a `bash` tool. A prompt-injected agent — or simply a model
curious about its environment — can read the password and emit it into a
tool result, which is sent to the model provider. The app password grants
account-level mail access if IMAP is enabled.

Fix: strip known-secret variables from the child environment before
spawning (`PIPELINE_NOTIFY_EMAIL_PASSWORD`, `PLANE_API_KEY`, any
`GH_TOKEN`/`GITHUB_TOKEN` present), or switch to an allowlist of the
`PIPELINE_*` variables agents actually need. The agent does not use either
credential; ticketing and notifications are pipeline-side.

### Low — `.pipeline.env` is world-readable

`-rw-r--r--` (0644) despite containing the SMTP app password. The dashboard
key file is correctly 0600. Fix: `chmod 600 .pipeline.env` (and consider
enforcing the mode at load, as `app/auth.py` already does for its key).

### Low — the dashboard does not validate the Host header

`app/dashboard.py` gates every `/api/*` route on a shared secret held in a
0600 file, compared with `secrets.compare_digest`, with a fixed generic 401
— that part is sound. The index route `/` injects the key into the page and
is deliberately exempt. There is no Host-header check, so a DNS-rebinding
page could reach `/` under an attacker-controlled hostname and read the key
out of the page. The server binds `127.0.0.1` by default
(`scripts/dashboard.sh:50`), which makes rebinding the remaining vector.

Fix: reject requests whose `Host` is not `127.0.0.1`, `localhost` or `[::1]`
on `/` (and preferably app-wide). `app/chat.py` already avoids trusting
`Host` for its internal base URL; the same principle applies here.

### Accepted risk — the reviewer's bash tool is arbitrary shell execution

`app/ollama_prompt_utils.py:307` runs reviewer-generated commands with
`shell=True` in the reviewed worktree, bounded by a 30-minute timeout.
This is inherent to a tool-enabled reviewer and the same capability the
implementer already has; it is recorded rather than flagged. Untrusted
repository content can steer it, so the environment-stripping fix above
matters for this path too.

## Verified controls

- **No secret has ever been committed.** `git log --all -S` for the SMTP
  password and the Plane key returns nothing. `.pipeline.env`,
  `model_registry.local.json` and `.dashboard_api_key` are gitignored
  (`.gitignore:24,37,17`).
- **Dashboard authentication**: 0600 key generated with
  `secrets.token_urlsafe`, `compare_digest`, fixed 401 detail, middleware
  scoped to `/api/` by prefix so new routes are covered by construction,
  and the chat pass-through accepted only on the exact
  `/api/chat/stream` path.
- **Chat host handling**: the internal API base URL is derived from the
  ASGI `server` scope, never from the client-supplied `Host`
  (`app/chat.py:64-88`).
- **Email transport**: STARTTLS with `ssl.create_default_context()`
  (certificate and hostname verification mandatory), and only the exception
  type is logged — never its message, which could embed the password
  (`pipeline/notification_email.py:169-181`).
- **Secret masking**: the env-conflict reporter renders secret values as
  `***` (`pipeline/scheduler_daemon.py:814-821`), as does
  `config_provenance` for `PIPELINE_NOTIFY_EMAIL_PASSWORD`.
- **No `os.system`, `eval` or `exec`.** No tracked file contains a
  hardcoded credential.
- **Shell-outs**: the only `shell=True` sites are `remote_sync.py:128`
  (every interpolation `shlex.quote`d) and the reviewer tool above.
- **Boundaries**: plan/story keys are validated against
  `^[A-Za-z0-9._-]+$` (`pipeline/parsers.py:291`) so they cannot traverse
  paths; the story `files` list rejects absolute paths, backslashes and
  `..` components; `remote_exec` spec files are `mkstemp` (0600) and
  unlinked after reading.

## Recommended order

1. `dispatch.py` acceptance path guard (Medium).
2. Strip secrets from the agent environment (Medium).
3. `chmod 600 .pipeline.env` (Low, no code).
4. Dashboard Host validation (Low).
