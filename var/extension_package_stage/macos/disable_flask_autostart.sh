#!/usr/bin/env bash
set -euo pipefail

LAUNCH_AGENT_ID="com.printingbusiness.flask"
PLIST_PATH="$HOME/Library/LaunchAgents/${LAUNCH_AGENT_ID}.plist"

if [[ -f "$PLIST_PATH" ]]; then
  launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
  rm -f "$PLIST_PATH"
  echo "Removed macOS Flask LaunchAgent: $PLIST_PATH"
else
  echo "No macOS Flask LaunchAgent found at: $PLIST_PATH"
fi
