#!/bin/bash
# Invoked by the "立即刷新" menu item (SwiftBar runs this with refresh=true, so SwiftBar
# re-renders the plugin AFTER this script returns). We must NOT call
# swiftbar://refreshplugin?name=claude-usage ourselves: this script IS a menu action of
# the claude-usage plugin, and SwiftBar drops a refresh request for a plugin whose own
# action is still running (re-entrancy). That self-refresh is why clicking used to do
# nothing even though the same script works fine from a terminal.
#
# So: just force a real /usage call now and write the fresh payload to last_fetch.json.
# --force bypasses fetch_usage.py's 5-minute throttle (which only guards the auto ticks).
# When we exit, SwiftBar's refresh=true re-runs the plugin, which renders this fresh data.
DIR="$HOME/claude-usage-widget"
LOG="$DIR/force_refresh.log"

write_json_atomically() {
  local target="$1"
  shift
  local tmp
  tmp="$(mktemp "$DIR/.${target##*/}.XXXXXX")" || return 1
  "$@" > "$tmp"
  local rc=$?
  if [ -s "$tmp" ]; then
    mv "$tmp" "$target"
  else
    rm -f "$tmp"
  fi
  return "$rc"
}

write_json_atomically "$DIR/last_fetch.json" python3 "$DIR/fetch_usage.py" --force 2>>"$LOG"
rc=$?
write_json_atomically "$DIR/codex_last_fetch.json" python3 "$DIR/fetch_codex_usage.py" 2>>"$LOG"
codex_rc=$?
# Kimi Code usage comes from Kimi's official billing API via the desktop
# session; --force bypasses its 5-minute throttle like the Claude fetch above.
write_json_atomically "$DIR/kimi_last_fetch.json" python3 "$DIR/fetch_kimi_usage.py" --force 2>>"$LOG"
kimi_rc=$?
# Heartbeat so a click always leaves a trace (helps diagnose if it ever misbehaves again).
echo "$(TZ=Asia/Shanghai date '+%F %T') Asia/Shanghai (UTC+8) force_refresh claude_rc=$rc codex_rc=$codex_rc kimi_rc=$kimi_rc bytes=$(wc -c < "$DIR/last_fetch.json" | tr -d ' ')" >> "$LOG"
# Keep the log from growing without bound.
tail -n 50 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
# Either side may be temporarily unavailable; SwiftBar should still render the other.
exit 0
