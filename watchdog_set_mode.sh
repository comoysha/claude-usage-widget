#!/bin/bash
# SwiftBar 菜单动作：切换任务看门狗模式。param1 = apply | dry
DIR="$(cd "$(dirname "$0")" && pwd)"
case "$1" in
  apply|dry) printf '%s' "$1" > "$DIR/watchdog_mode.txt.tmp" && mv "$DIR/watchdog_mode.txt.tmp" "$DIR/watchdog_mode.txt" ;;
esac
