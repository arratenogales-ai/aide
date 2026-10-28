#!/bin/zsh
# Stops Aide and removes the LaunchAgent (running tasks are paused and resume when you install it again).
set -euo pipefail
DESTINATION="$HOME/Library/LaunchAgents/com.aide.agent.plist"
launchctl bootout "gui/$(id -u)/com.aide.agent" 2>/dev/null || true
rm -f "$DESTINATION"
echo "Uninstalled."
