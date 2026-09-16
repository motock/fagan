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

# --- External tools (the pipeline shells out to these) ---------------------
# Delegated to the pre-venv doctor module: runs on the system python before
# the venv exists; git/gh/claude required, ollama/docker optional.
"${PYTHON:-python3}" "$ROOT/scripts/install_checks.py"

if [ ! -x "$PYBIN" ]; then
  echo "==> Creating venv at $VENV"
  "$PY" -m venv "$VENV"
fi

echo "==> Installing $REQ"
"$PYBIN" -m pip install --quiet --upgrade pip
"$PYBIN" -m pip install --quiet -r "$ROOT/$REQ"

# Dashboard deps (fastapi, uvicorn): required by scripts/dashboard.sh and now
# installed by default. pip install is idempotent, so re-runs are safe; on
# --dev this is a harmless duplicate because requirements-dev.txt already
# includes requirements-dashboard.txt.
"$PYBIN" -m pip install --quiet -r "$ROOT/requirements-dashboard.txt"
echo "==> Python deps installed:"
"$PYBIN" -m pip list 2>/dev/null | grep -iE '^(mcp|httpx|pytest) ' || true

echo
echo "==> Done. Next steps:"
echo "  - Register this MCP server (globally or in a project .mcp.json)."
echo "  - Set required env vars (see README: Configuration / Prerequisites),"
echo "    e.g. PLANE_*, REPO_ROOT, and any PIPELINE_BACKEND_*=local routing."
echo "  - Or choose a provider per role interactively:"
echo "    .venv/bin/python scripts/choose_providers.py"
echo "  - Run the tests (after a --dev install):  .venv/bin/python -m pytest -q"
