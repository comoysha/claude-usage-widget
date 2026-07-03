#!/bin/bash
# <xbar.title>Claude Code Usage Pace</xbar.title>
# <xbar.version>0.1</xbar.version>
# <xbar.desc>并列显示 5 小时窗口的官方用量% 与已过时间%，判断烧钱节奏。</xbar.desc>
# <xbar.dependencies>python3,curl,jq</xbar.dependencies>
# SwiftBar refreshes on the filename interval (5m) — matches fetch_usage.py's 5-minute
# /usage throttle, so every tick makes a real API call. Renders via the helper below.

DIR="$HOME/claude-usage-widget"
# fetch_usage.py prints the usage JSON on success, or {"error": ...} on failure.
# Capture the latest result (success OR error) so the widget reflects this tick,
# not a stale cache. fetch_usage.py itself only caches usage_raw.json on success.
python3 "$DIR/fetch_usage.py" 2>/dev/null > "$DIR/last_fetch.json"
# render.py reads last_fetch.json and emits SwiftBar markup (never touches tokens).
python3 "$DIR/render.py"
