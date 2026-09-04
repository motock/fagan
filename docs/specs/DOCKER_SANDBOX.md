# Docker Sandbox

## 1. Opt‑in contract

Sandboxing is **off by default**.  To enable it, set the environment variable
`PIPELINE_SANDBOX` to one of the allowed values:

| Value | Meaning |
|-------|---------|
| `none` | No sandboxing; the agent runs directly on the host. |
| `docker` | Wrap the agent in a Docker container. |

If `PIPELINE_SANDBOX` is unset, empty, or contains only whitespace, the
default value `none` is used.

## 2. Required image variable

When `PIPELINE_SANDBOX=docker` the variable `PIPELINE_SANDBOX_IMAGE` **must** be
set to the image name to use.  If it is missing or empty a
`ValueError` is raised:

```
ValueError: PIPELINE_SANDBOX_IMAGE is not set: refusing to build a docker command without an image (set the variable to the container image to use)
```

## 3. Container shape

The Docker command built by `build_docker_command` has the following shape:

```
['docker', 'run', '--rm', '-v', f'{worktree}:{worktree}', '--workdir', worktree, *env_flags, image, *argv]
```

* The worktree is volume‑mounted **at its identical host path** (`-v
  worktree:worktree`).  This means relative paths inside the container refer to
  the same files as on the host.
* `--workdir` is set to the worktree path.
* The image is taken from `PIPELINE_SANDBOX_IMAGE`.
* The agent’s original `argv` is appended verbatim.

## 4. Environment passthrough policy

The sandbox is **deny-by-default**.  Only environment variables whose names start
with `LOCAL_AGENT_` or `PIPELINE_` are forwarded into the container via `-e
key=value`.  All other host variables—including credentials—are **not forwarded**.

The allowlist is absolute: the only way to forward a variable is to name it explicitly with a `LOCAL_AGENT_` or `PIPELINE_` prefix. There is no other escape hatch — a non‑prefixed key added to the `env` dictionary passed to `spawn_harness` is silently dropped by both the pre‑filter in `spawn_harness` and the filter inside `build_docker_command`.

## 5. Fail‑closed behavior matrix

| Trigger condition | Exception type | Observable result |
|-------------------|----------------|-------------------|
| `PIPELINE_SANDBOX` set to an unknown value | `ValueError` | Sandbox resolution fails before any process is spawned |
| `PIPELINE_SANDBOX=docker` but `docker` binary missing | `RuntimeError` | Dispatch is refused; **no unsandboxed fallback** |
| `PIPELINE_SANDBOX=docker` and `PIPELINE_SANDBOX_IMAGE` unset or empty | `ValueError` | Docker command construction fails |

All failures are *fail‑closed*: the harness never falls back to unsandboxed
execution.

## 6. What is NOT isolated

* **Network access** – the container shares the host’s network namespace by
  default, so it can reach external services.
* **Host resources** – the container can access the host’s file system
  outside the worktree via the Docker daemon’s default settings.
* **Other containers** – the sandbox does not prevent the container from
  interacting with other running containers.

In short, the sandbox only guarantees that the agent’s working directory is
mounted inside the container; it is not a hard security boundary.

## 7. Live‑host validation note

The test suite mocks the Docker binary.  To validate the sandbox on a real
host you must perform the following one‑time checks:

1. **Docker binary absent** – run with `PIPELINE_SANDBOX=docker` and ensure the
   Docker binary is not on the PATH.  The dispatch should raise a
   `RuntimeError` and nothing should be executed.
2. **Docker binary present** – run with `PIPELINE_SANDBOX=docker` and a valid
   `PIPELINE_SANDBOX_IMAGE`.  The container starts, the agent runs inside the
   mounted worktree, and the output should include the measured `docker --version`
   next to the expected value.

These checks confirm that the sandbox behaves correctly on the live host.

The live‑host validation requirement is documented in
[.claude/rules/testing-config-gates.md](.claude/rules/testing-config-gates.md).

## 8. Relationship to remote execution

Docker sandboxing applies only to the **local** spawn branch (`spawn_harness`
with `mode == "local"`).  Remote/SSH execution is governed by the
[Remote Execution](docs/specs/REMOTE_EXECUTION.md) specification.
