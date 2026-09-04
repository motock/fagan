# Remote Execution (SSH)

## Status

Remote/SSH execution is **not implemented yet**.  The trigger is `PIPELINE_EXEC_*` resolving to `ssh` mode via `resolve_execution_mode` (`pipeline/execution.py:27`).  Attempting to use SSH execution raises:

```python
NotImplementedError("ssh execution is not implemented yet (B1 later story)")
```
