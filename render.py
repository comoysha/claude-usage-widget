#!/usr/bin/env python3
"""Render SwiftBar output from usage_raw.json using iOS-style PNGs (imggen.py).

Menubar  = two-line icon (time% over usage%).
Dropdown = panel with two rounded progress bars + pace verdict + reset countdown.
Defensive field parsing; degrades to a muted text state on any error. No tokens.
"""
import base64
import json
import time
from pathlib import Path

import imggen

HERE = Path(__file__).resolve().parent
# latest fetch result (usage payload on success, {"error":...} on failure);
# falls back to the last successful cache if the tick file is missing.
RAW = HERE / "last_fetch.json"
CACHE = HERE / "usage_raw.json"
WINDOW_S = 5 * 3600
SEVEN_S = 7 * 86400


def pct_of(v):
    if v is None:
        return None
    v = float(v)
    return v * 100 if v <= 1.0 else v


def parse_time(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e12 else float(v)
    s = str(v).strip().replace("Z", "+00:00")
    try:
        from datetime import datetime
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def find_window(data, names):
    """Extract (used_pct, reset_epoch) for a usage window block (five_hour / seven_day)."""
    if not isinstance(data, dict):
        return None, None
    blk = None
    for n in names:
        if isinstance(data.get(n), dict):
            blk = data[n]; break
    if blk is None:
        return None, None
    used = None
    for k in ("utilization", "used_pct", "usedPercent", "percent_used", "usage"):
        if k in blk:
            used = pct_of(blk[k]); break
    reset = None
    for k in ("resets_at", "resetsAt", "reset_at", "resetAt"):
        if k in blk:
            reset = parse_time(blk[k]); break
    return used, reset


def fmt_rem(seconds):
    """Human countdown: days+hours past a day, else hours+minutes."""
    s = int(max(0, seconds))
    if s >= 86400:
        return "%dd%02dh" % (s // 86400, (s % 86400) // 3600)
    return "%dh%02dm" % (s // 3600, (s % 3600) // 60)


def verdict_of(used, time_pct):
    """Pace verdict for one window, from its own usage-vs-time gap.

    d = used - time:  d>0 → burning faster than the clock (tokens getting
    scarce while time remains); d<0 → spending slower than the clock (token
    surplus with time running short). Returns (text, color)."""
    col = imggen.pace_color(used or 0, time_pct)
    if time_pct is None or used is None:
        return "时间未知", col
    d = used - time_pct
    if d > 15:
        return "↑ 烧超前 · 悠着点", col
    if d > 5:
        return "↑ 略超前 · 省着点", col
    if d < -5:
        return "↓ 落后 · 有余量", col
    return "≈ 跟上节奏", col


def window_stats(used, reset, span_s, now):
    """Return {used, time, rem, ucol, verdict, vcol} for a window."""
    if reset:
        remaining = max(0, reset - now)
        time_pct = max(0.0, min(100.0, (span_s - remaining) / span_s * 100))
        rem_txt = fmt_rem(remaining)
    else:
        time_pct, rem_txt = None, "—"
    verdict, vcol = verdict_of(used, time_pct)
    return {"used": used, "time": time_pct, "rem": rem_txt,
            "ucol": imggen.pace_color(used or 0, time_pct),
            "verdict": verdict, "vcol": vcol}


def b64(path):
    return base64.b64encode(Path(path).read_bytes()).decode()


def muted(title, detail):
    print("%s | color=#8E8E93" % title)
    print("---")
    print("%s | color=#8E8E93" % detail)
    print("立即刷新（强制调接口）| bash=%s/force_refresh.sh terminal=false refresh=false" % HERE)


def main():
    try:
        data = json.loads(RAW.read_text())
    except Exception:
        try:
            data = json.loads(CACHE.read_text())
        except Exception:
            return muted("Claude ·无数据", "还没抓到用量数据")

    if isinstance(data, dict) and data.get("error"):
        err = str(data["error"])
        short = ("限流冷却" if "rate_limit" in err
                 else "需重登" if "需重登" in err or "401" in err or "auth" in err.lower()
                 else "离线")
        return muted("Claude ·" + short, ("错误: " + err)[:120])

    used, reset = find_window(data, ("five_hour", "fiveHour"))
    if used is None:
        keys = ", ".join(list(data.keys())[:8]) if isinstance(data, dict) else "?"
        return muted("Claude ·待确认字段", "顶层键: " + keys)

    now = time.time()
    five = window_stats(used, reset, WINDOW_S, now)
    sd_used, sd_reset = find_window(data, ("seven_day", "sevenDay"))
    seven = window_stats(sd_used, sd_reset, SEVEN_S, now)

    time_pct = five["time"]
    mb = imggen.menubar_icon(used, time_pct)
    pn = imggen.panel(five, seven)

    print(" | image=%s" % b64(mb))
    print("---")
    print("| image=%s" % b64(pn))
    print("---")
    print("立即刷新（强制调接口）| bash=%s/force_refresh.sh terminal=false refresh=false" % HERE)


if __name__ == "__main__":
    main()
