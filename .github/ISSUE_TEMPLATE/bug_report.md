---
name: Bug report
about: Something in the pipeline behaved unexpectedly
title: ""
labels: bug
assignees: ""
---

**Before filing:** if this looks like a security vulnerability, please use
[SECURITY.md](../../SECURITY.md)'s private reporting path instead of a public
issue.

## What happened

A clear description of the observed behavior.

## What you expected

What you expected to happen instead.

## Steps to reproduce

1. ...
2. ...

## Environment

- OS (macOS / Linux / other):
- Python version (`python3 --version`):
- Backend(s) in use (`claude` / `ollama` / `lmstudio` / `mlx` / `litellm`):
- Autonomy level (`PIPELINE_AUTONOMY`):
- Relevant output of `git log -1 --format=%H` (commit you're on):

## Logs / evidence

Paste the relevant excerpt from `agent.log`, `advance-scheduler.log`, the
dashboard, or `pytest` output. Redact anything sensitive (API keys, tokens,
file paths outside this repo).
