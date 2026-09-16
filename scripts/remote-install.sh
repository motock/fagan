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

# Success summary.
echo
echo "==> Installed to $FAGAN_INSTALL_DIR"
echo "See $FAGAN_INSTALL_DIR/README.md for next steps"
