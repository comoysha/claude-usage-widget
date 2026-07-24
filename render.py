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
CODEX_RAW = HERE / "codex_last_fetch.json"
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
    print("立即刷新（强制调接口）| bash=%s/force_refresh.sh terminal=false refresh=true" % HERE)


def codex_windows(data):
    if not isinstance(data, dict) or data.get("error"):
        return None
    limits = data.get("rate_limits")
    if not isinstance(limits, dict):
        return None
    windows = {}
    def convert(name):
        block = limits.get(name)
        if not isinstance(block, dict):
            return
        minutes = block.get("window_minutes")
        if minutes:
            # Codex app-server reports usedPercent as 0..100 (1 means 1%),
            # while Claude's API may report utilization as 0..1. Do not use
            # pct_of() here: its fraction heuristic turns exactly 1 into 100.
            used = block.get("used_percent")
            try:
                used = max(0.0, min(100.0, float(used))) if used is not None else None
            except (TypeError, ValueError):
                used = None
            windows[int(minutes)] = (used,
                                     parse_time(block.get("resets_at")), int(minutes))
    convert("primary")
    convert("secondary")
    return windows


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
    try:
        codex_data = json.loads(CODEX_RAW.read_text())
    except Exception:
        codex_data = None
    cw = codex_windows(codex_data)
    codex_seven = None
    if cw is not None:
        su, sr, sm = cw.get(10080, (None, None, 10080))
        codex_seven = window_stats(su, sr, sm * 60, now)

    mb = imggen.combined_menubar_icon(
        (five["used"], five["time"]),
        (seven["used"], seven["time"]),
        ((codex_seven or {}).get("used"), (codex_seven or {}).get("time")),
    )
    claude_panel = imggen.panel(five, seven, "Claude Code", "claude_panel.png")

    print(" | image=%s" % b64(mb))
    print("---")
    print("| image=%s" % b64(claude_panel))
    print("---")
    if codex_seven:
        codex_panel = imggen.panel(None, codex_seven, "Codex", "codex_panel.png", only_weekly=True)
        print("| image=%s" % b64(codex_panel))
    else:
        detail = (codex_data or {}).get("error", "还没有本地用量快照") if isinstance(codex_data, dict) else "还没有本地用量快照"
        print("Codex · 无数据 | color=#8E8E93")
        print("--%s | color=#8E8E93" % str(detail)[:100])
    print("---")
    print("立即刷新 | bash=%s/force_refresh.sh terminal=false refresh=true" % HERE)


if __name__ == "__main__":
    main()
