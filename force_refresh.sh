#!/bin/bash
# Invoked by the "立即刷新" menu item. Unlike a plain refresh=true (which re-runs the
# plugin but is still subject to fetch_usage.py's 5-minute throttle), this forces a real
# /usage API call now, then tells SwiftBar to re-render the plugin once the call returns.
DIR="$HOME/claude-usage-widget"
python3 "$DIR/fetch_usage.py" --force 2>/dev/null > "$DIR/last_fetch.json"
# Refresh only after the fetch completes, so we render fresh data (not a race with the
# background fetch). Plugin name = filename minus interval/extension: claude-usage.5m.sh.
open -g "swiftbar://refreshplugin?name=claude-usage" 2>/dev/null
