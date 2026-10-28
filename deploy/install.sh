#!/bin/zsh
# Installs Aide as a user LaunchAgent: it starts when you log in and restarts if it crashes.
# Usage: ./deploy/install.sh   (from any folder)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
DESTINATION="$HOME/Library/LaunchAgents/com.aide.agent.plist"
if [[ ! -x "$PYTHON" ]]; then
  echo "Can't find $PYTHON. First run: cd \"$ROOT\" && uv sync" >&2
  exit 1
fi
"$PYTHON" -m aide --check || { echo "The startup check failed; read the warnings above." >&2; exit 1; }
mkdir -p "$ROOT/data/logs" "$HOME/Library/LaunchAgents"
sed -e "s#__PYTHON__#$PYTHON#g" -e "s#__ROOT__#$ROOT#g" -e "s#__BIN__#$ROOT/.venv/bin#g" \
  "$ROOT/deploy/template.plist" > "$DESTINATION"
plutil -lint "$DESTINATION"
launchctl bootout "gui/$(id -u)/com.aide.agent" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DESTINATION"
echo "Installed. Status:"
launchctl print "gui/$(id -u)/com.aide.agent" | grep -E "state|pid" | head -3
