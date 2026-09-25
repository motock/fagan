#!/usr/bin/env bash
# One-line installer for Fagan.
# Usage: curl -fsSL <raw-url> | bash
#        or: bash remote-install.sh
set -euo pipefail

# Default values, overridable via environment.
FAGAN_INSTALL_DIR="${FAGAN_INSTALL_DIR:-$HOME/.fagan}"
FAGAN_REPO_URL="${FAGAN_REPO_URL:-https://github.com/motock/fagan.git}"

# Ensure git is available.
if ! command -v git >/dev/null 2>&1; then
  echo "remote-install: git not found" >&2
  exit 1
fi

# Decide action based on target directory.
if [ ! -e "$FAGAN_INSTALL_DIR" ]; then
  echo "==> Cloning $FAGAN_REPO_URL to $FAGAN_INSTALL_DIR"
  git clone "$FAGAN_REPO_URL" "$FAGAN_INSTALL_DIR"
elif [ -e "$FAGAN_INSTALL_DIR/.git" ]; then
  origin=$(git -C "$FAGAN_INSTALL_DIR" remote get-url origin 2>/dev/null || true)
  if [ "$origin" != "$FAGAN_REPO_URL" ]; then
    echo "remote-install: $FAGAN_INSTALL_DIR already exists as a git repo with a different origin ($origin != $FAGAN_REPO_URL). Refusing to touch it. Set FAGAN_INSTALL_DIR to a different path." >&2
    exit 1
  fi
  echo "==> Updating existing install at $FAGAN_INSTALL_DIR"
  git -C "$FAGAN_INSTALL_DIR" pull --ff-only
else
  echo "remote-install: $FAGAN_INSTALL_DIR already exists and is not a git repository. Refusing to touch it. Set FAGAN_INSTALL_DIR to a different path." >&2
  exit 1
fi

# Verify install script exists.
INSTALLER="$FAGAN_INSTALL_DIR/scripts/install.sh"
if [ ! -f "$INSTALLER" ]; then
  echo "remote-install: missing install script at $INSTALLER" >&2
  exit 1
fi

# Run the install script.
echo "==> Running install.sh"
bash "$INSTALLER"

# Copy persona subagents and the decision policy into ~/.claude, never overwriting.
copy_if_absent() {
  if [ -e "$2" ]; then
    echo "    kept existing $2"
  else
    cp "$1" "$2"
    echo "    installed $2"
  fi
}
if [ -d "$FAGAN_INSTALL_DIR/agents" ]; then
  echo "==> Installing persona subagents into $HOME/.claude/agents"
  mkdir -p "$HOME/.claude/agents"
  for src in "$FAGAN_INSTALL_DIR"/agents/*.md; do
    copy_if_absent "$src" "$HOME/.claude/agents/$(basename "$src")"
  done
fi
if [ -f "$FAGAN_INSTALL_DIR/overlord-policy.md" ]; then
  copy_if_absent "$FAGAN_INSTALL_DIR/overlord-policy.md" "$HOME/.claude/overlord-policy.md"
fi

# Register the MCP server with Claude Code, unless it is already registered.
PY="$FAGAN_INSTALL_DIR/.venv/bin/python3"
SERVER="$FAGAN_INSTALL_DIR/app/pipeline_mcp_server.py"
MANUAL="claude mcp add -s user pipeline \"$PY\" \"$SERVER\""
if [ -x "$PY" ] && [ -f "$SERVER" ]; then
  if ! command -v claude >/dev/null 2>&1; then
    echo "remote-install: claude CLI not found; once installed, run: $MANUAL" >&2
  elif claude mcp get pipeline >/dev/null 2>&1; then
    echo "==> MCP server 'pipeline' already registered with Claude Code; left unchanged"
  elif claude mcp add -s user pipeline "$PY" "$SERVER"; then
    echo "==> Registered MCP server 'pipeline' with Claude Code"
  else
    echo "remote-install: MCP registration failed; run it by hand: $MANUAL" >&2
  fi
fi

# Success summary.
echo
echo "==> Installed to $FAGAN_INSTALL_DIR"
echo "See $FAGAN_INSTALL_DIR/README.md for next steps"
