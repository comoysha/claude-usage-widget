#!/bin/bash
# <xbar.title>Claude + Codex + Kimi + Cursor Usage Pace</xbar.title>
# <xbar.version>0.2</xbar.version>
# <xbar.desc>并列显示各订阅窗口的官方用量% 与已过时间%，判断烧钱节奏（含 Cursor G7d）。</xbar.desc>
# <xbar.dependencies>python3,curl,jq</xbar.dependencies>
# SwiftBar refreshes on the filename interval (5m) — matches fetch_usage.py's 5-minute
# /usage throttle, so every tick makes a real API call. Renders via the helper below.

DIR="$HOME/claude-usage-widget"

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

# fetch_usage.py prints the usage JSON on success, or {"error": ...} on failure.
# Capture the latest result (success OR error) so the widget reflects this tick,
# not a stale cache. fetch_usage.py itself only caches usage_raw.json on success.
write_json_atomically "$DIR/last_fetch.json" python3 "$DIR/fetch_usage.py" 2>/dev/null
# Codex app-server exposes its official rate limits; the reader falls back to local
# session events and never reads auth.json or copies credentials.
write_json_atomically "$DIR/codex_last_fetch.json" python3 "$DIR/fetch_codex_usage.py" 2>/dev/null
# Kimi Code's official 5h/7d windows via the Kimi desktop session (read-only).
write_json_atomically "$DIR/kimi_last_fetch.json" python3 "$DIR/fetch_kimi_usage.py" 2>/dev/null
# Cursor / Grok shared weekly pool via the Cursor desktop access token (read-only).
write_json_atomically "$DIR/cursor_last_fetch.json" python3 "$DIR/fetch_cursor_usage.py" 2>/dev/null
# render.py reads last_fetch.json and emits SwiftBar markup (never touches tokens).
python3 "$DIR/render.py"
