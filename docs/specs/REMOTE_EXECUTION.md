# Remote Execution (SSH)

## Status

Remote/SSH execution is **implemented** (B1). The trigger is `PIPELINE_EXEC_*`
resolving to `ssh` mode via `resolve_execution_mode` (`pipeline/execution.py`).

`spawn_harness` dispatches ssh mode to `_spawn_ssh`, which invokes the
`pipeline.remote_exec` supervisor (`python -m pipeline.remote_exec`) with the
flags `--worktree`, `--remote-url`, `--host`, and `--spec-file`. ssh mode
deliberately bypasses local docker-sandbox resolution: the harness runs on the
ssh host, so no sandbox image or pinning applies.

Before B1 landed, attempting to use SSH execution raised:

```python
NotImplementedError("ssh execution is not implemented yet (B1 later story)")
```

That stub is retained here as historical ground truth for the pre-B1 behavior;
it is no longer raised.

## Configuration

Fail closed — a missing value raises `ValueError`, never a silent fallback to
local execution on the orchestrator host:

- `PIPELINE_REMOTE_EXEC_HOST` — target ssh host (e.g. `gpu-box.example.com`).
- `PIPELINE_REMOTE_SYNC_ROOT` — absolute path to the bare-repo root on the
  host (e.g. `/srv/git`). The remote URL is composed as
  `ssh://<host><sync_root>/repo-bare.git` and the remote worktree as
  `<sync_root>/worktrees/<worktree-name>`.
