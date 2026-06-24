#!/usr/bin/env bash
#
# Install/refresh the pipeline MCP server's Python environment and report on
# the external tools it relies on. Idempotent — safe to re-run.
#
#   scripts/install.sh           # runtime deps only
#   scripts/install.sh --dev     # also install the test deps (pytest)
#
# This sets up the local .venv (gitignored) the server runs from. It does NOT
# register the MCP server or set environment variables — see the README
# (Prerequisites / Configuration) for those steps.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
PYBIN="$VENV/bin/python3"
REQ="requirements.txt"
[ "${1:-}" = "--dev" ] && REQ="requirements-dev.txt"

echo "==> Pipeline install ($ROOT)"

# --- Python venv -----------------------------------------------------------
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python 3.10+ (the mcp SDK needs it) and re-run." >&2
  exit 1
fi
PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "==> Using $PY ($PYVER)"
"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,10) else 1)' || {
  echo "ERROR: Python 3.10+ required (found $PYVER)." >&2; exit 1; }

if [ ! -x "$PYBIN" ]; then
  echo "==> Creating venv at $VENV"
  "$PY" -m venv "$VENV"
fi

echo "==> Installing $REQ"
"$PYBIN" -m pip install --quiet --upgrade pip
"$PYBIN" -m pip install --quiet -r "$ROOT/$REQ"
echo "==> Python deps installed:"
"$PYBIN" -m pip list 2>/dev/null | grep -iE '^(mcp|httpx|pytest) ' || true

# --- External tools (the pipeline shells out to these) ---------------------
echo
echo "==> External tool check (the pipeline calls these directly):"
check() {  # name, hint, required-for
  if command -v "$1" >/dev/null 2>&1; then
    echo "  [ok]   $1 — $(command -v "$1")"
  else
    echo "  [MISS] $1 — needed for $3. $2"
  fi
}
check git    "install git"                                 "worktrees, branches, merges"
check gh     "https://cli.github.com ; then 'gh auth login'" "the review gate (PR create) and merges"
check claude "install the Claude Code CLI"                 "any role routed to the 'claude' backend (default)"

echo
echo "==> Local backend (optional — only if PIPELINE_BACKEND_*=local):"
if command -v ollama >/dev/null 2>&1; then
  echo "  [ok]   ollama — $(command -v ollama)"
  MODEL="${PIPELINE_LOCAL_MODEL_DEFAULT:-devstral:24b}"
  # Capture first, then grep: piping `ollama list | grep -q` directly trips
  # `set -o pipefail` (grep -q closes the pipe early -> ollama list takes
  # SIGPIPE -> the pipeline reports failure even on a match).
  INSTALLED_MODELS="$(ollama list 2>/dev/null || true)"
  if printf '%s\n' "$INSTALLED_MODELS" | grep -qF "$MODEL"; then
    echo "  [ok]   model '$MODEL' present"
  else
    echo "  [todo] model '$MODEL' not pulled — run: ollama pull $MODEL"
  fi
else
  echo "  [skip] ollama not found — install from https://ollama.com only if you"
  echo "         want to route dispatch/review/overlord to a local model."
fi

echo
echo "==> Done. Next steps:"
echo "  - Register this MCP server (globally or in a project .mcp.json)."
echo "  - Set required env vars (see README: Configuration / Prerequisites),"
echo "    e.g. PLANE_*, REPO_ROOT, and any PIPELINE_BACKEND_*=local routing."
echo "  - Run the tests (after a --dev install):  .venv/bin/python -m pytest -q"
