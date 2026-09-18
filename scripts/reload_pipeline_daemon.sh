#!/usr/bin/env bash
# Reload the running pipeline advance-scheduler daemon.
#
# Drift warning: The launchd plist in the repo is a TEMPLATE. The running daemon reads only the
# installed agent under ~/Library/LaunchAgents/.  Edit the installed plist
# surgically, then reload with this script (or equivalently `launchctl unload`
# then `launchctl load` on that path).  The change has NO effect until this
# reload happens.
#
# WARNING: Never regenerate-and-install wholesale from launchd/*.plist.template
# without diffing the installed file first, because the installed copy carries
# local overrides the template does not.
set -euo pipefail

PLIST="${HOME}/Library/LaunchAgents/com.fagan.pipeline.advance-scheduler.plist"
LABEL="com.fagan.pipeline.advance-scheduler"

# Verify the plist exists.
if [[ ! -f "$PLIST" ]]; then
    echo "Error: required plist not found: $PLIST" >&2
    exit 1
fi

# Unload the job.  If it is not loaded, ignore the error.
launchctl unload "$PLIST" 2>/dev/null || true

# Load the job.  Fail loudly if this fails.
launchctl load "$PLIST"

# Resolve the PID from the label.
PID=$(launchctl list | awk -v lbl="$LABEL" '$3==lbl{print $1}')
if [[ -z "$PID" || "$PID" == "-" ]]; then
    echo "Error: no running daemon after load" >&2
    exit 1
fi

# Dump the environment of the running process, filtering for PIPELINE_ variables.
ps eww "$PID" | tr ' ' '\n' | grep -E '^PIPELINE_' || echo "(no PIPELINE_ variables found in the running process environment)"
