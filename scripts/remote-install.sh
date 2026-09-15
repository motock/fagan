#!/usr/bin/env bash
# Fetches and installs Fagan by cloning the repo and running scripts/install.sh.
# Usage: curl -fsSL <raw-url-to-this-file> | bash
#        or: bash remote-install.sh [install.sh args, e.g. --dev]
set -euo pipefail

REPO_URL="${FAGAN_REPO_URL:-https://github.com/motock/fagan.git}"
INSTALL_DIR="${FAGAN_INSTALL_DIR:-$HOME/.fagan}"

if ! command -v git >/dev/null 2>&1; then
  echo "ERROR: git not found. Install git and re-run." >&2
  exit 1
fi

if [ -d "$INSTALL_DIR" ]; then
  if [ -d "$INSTALL_DIR/.git" ]; then
    origin="$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || true)"
    if [ "$origin" != "$REPO_URL" ]; then
      echo "ERROR: $INSTALL_DIR already exists as a git repo with a different origin ($origin != $REPO_URL). Refusing to touch it. Set FAGAN_INSTALL_DIR to a different path." >&2
      exit 1
    fi
    echo "==> Updating existing install at $INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only
  else
    echo "ERROR: $INSTALL_DIR already exists and is not a git repository. Refusing to touch it. Set FAGAN_INSTALL_DIR to a different path." >&2
    exit 1
  fi
else
  echo "==> Cloning $REPO_URL to $INSTALL_DIR"
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

echo "==> Running install.sh"
"$INSTALL_DIR/scripts/install.sh" "$@"

echo
echo "==> Installed to $INSTALL_DIR"
echo "Next: cd \"$INSTALL_DIR\" and continue from the README Quickstart's"
echo "step 2 (register the MCP server)."