#!/usr/bin/env python3
"""Render SwiftBar output from usage_raw.json using iOS-style PNGs (imggen.py).

Menubar  = two-line icon (time% over usage%).
Dropdown = panel with two rounded progress bars + pace verdict + reset countdown.
Defensive field parsing; degrades to a muted text state on any error. No tokens.
"""
import base64
import json
import sys
import time
from pathlib import Path

import imggen
from PIL import Image

HERE = Path(__file__).resolve().parent
# latest fetch result (usage payload on success, {"error":...} on failure);
# falls back to the last successful cache if the tick file is missing.
RAW = HERE / "last_fetch.json"
CACHE = HERE / "usage_raw.json"
CODEX_RAW = HERE / "codex_last_fetch.json"
KIMI_RAW = HERE / "kimi_last_fetch.json"
WINDOW_S = 5 * 3600
SEVEN_S = 7 * 86400


def pct_of(v):
    if v is None:
        return None
    # Claude's usage endpoint reports utilization in percentage points:
    # 1.0 means 1%, not a 0..1 fraction. The old fraction heuristic turned
    # exactly 1% into 100% and made the widget disagree with Claude's UI.
    return max(0.0, min(100.0, float(v)))


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
            try:
                used = pct_of(blk[k])
            except (TypeError, ValueError):
                used = None
            break
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
    return {"used": used, "time": time_pct, "rem": rem_txt, "reset": reset,
            "ucol": imggen.pace_color(used or 0, time_pct),
            "verdict": verdict, "vcol": vcol}


def b64(path):
    return base64.b64encode(Path(path).read_bytes()).decode()


def panel_image_line(path):
    """Render a Retina panel at its intended point size in SwiftBar 2.1+."""
    with Image.open(path) as image:
        dpi = image.info.get("dpi", (144, 144))
        dpi_x = dpi[0] or 144
        dpi_y = dpi[1] or 144
        width = image.width * 72 / dpi_x
        height = image.height * 72 / dpi_y
    return "| image=%s width=%.1f height=%.1f" % (b64(path), width, height)


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


def send_usage_alerts(windows, now, dry_run=False):
    """任一窗口用满 100% 时发飞书推送；推送出任何问题都不能影响菜单栏显示。"""
    try:
        import usage_alerts
        items = []
        for name, window, st in windows:
            if st is None:
                continue
            reset = st.get("reset")
            items.append({"key": "%s|%s" % (name, window), "name": name,
                          "window": window, "used": st["used"], "reset": reset})
        usage_alerts.check(items, now=now, dry_run=dry_run)
    except Exception:
        pass


def main():
    try:
        data = json.loads(RAW.read_text())
    except Exception:
        try:
            data = json.loads(CACHE.read_text())
        except Exception:
            data = None

    used, reset = find_window(data, ("five_hour", "fiveHour"))
    now = time.time()
    claude_available = used is not None and not (
        isinstance(data, dict) and data.get("error")
    )
    five = window_stats(used, reset, WINDOW_S, now) if claude_available else None
    sd_used, sd_reset = find_window(data, ("seven_day", "sevenDay"))
    seven = window_stats(sd_used, sd_reset, SEVEN_S, now) if claude_available else None

    try:
        codex_data = json.loads(CODEX_RAW.read_text())
    except Exception:
        codex_data = None
    cw = codex_windows(codex_data)
    codex_five = codex_seven = None
    # A free plan means the paid Codex subscription has lapsed: hide Codex
    # entirely (menubar column and dropdown panel) instead of showing a dead 0%.
    plan = ((codex_data or {}).get("rate_limits") or {}).get("plan_type") \
        if isinstance(codex_data, dict) else None
    codex_hidden = str(plan).lower() == "free"
    if cw is not None and not codex_hidden:
        su, sr, sm = cw.get(10080, (None, None, 10080))
        codex_seven = window_stats(su, sr, sm * 60, now)
        # The 5-hour window is plan-dependent; only show it when Codex reports it.
        if 300 in cw:
            fu, fr, fm = cw[300]
            codex_five = window_stats(fu, fr, fm * 60, now)

    # Kimi Code: the console shows exactly two bars — 5 小时用量 and
    # 总使用量 (the subscription-cycle quota, ~30 days). The cycle window
    # uses the same utilization/resets_at fields as Claude's payload, so
    # find_window works; its span comes from the subscription itself.
    try:
        kimi_data = json.loads(KIMI_RAW.read_text())
    except Exception:
        kimi_data = None
    kimi_err = (kimi_data or {}).get("error", "") if isinstance(kimi_data, dict) else ""
    k_used, k_reset = find_window(kimi_data, ("five_hour", "fiveHour"))
    kc_used, kc_reset = find_window(kimi_data, ("cycle", "monthly", "seven_day"))
    kc_start = parse_time(
        ((kimi_data or {}).get("cycle") or {}).get("starts_at")
    ) if isinstance(kimi_data, dict) else None
    kimi_available = not kimi_err and (k_used is not None or kc_used is not None)
    kimi_five = window_stats(k_used, k_reset, WINDOW_S, now) \
        if k_used is not None else None
    kimi_cycle = None
    if kc_used is not None:
        span_s = (kc_reset - kc_start) if (kc_reset and kc_start and kc_reset > kc_start) \
            else 30 * 86400
        kimi_cycle = window_stats(kc_used, kc_reset, span_s, now)

    send_usage_alerts(
        (("Claude", "5 小时", five), ("Claude", "7 天", seven),
         ("Codex", "5 小时", codex_five), ("Codex", "7 天", codex_seven),
         ("Kimi", "5 小时", kimi_five), ("Kimi", "订阅周期(30 天)", kimi_cycle)),
        now, dry_run="--alerts-dry-run" in sys.argv)

    mb = imggen.combined_menubar_icon(
        ((five or {}).get("used"), (five or {}).get("time")),
        ((seven or {}).get("used"), (seven or {}).get("time")),
        (codex_seven["used"], codex_seven["time"]) if codex_seven else None,
        show_claude=claude_available,
        codex_five=(codex_five["used"], codex_five["time"]) if codex_five else None,
        kimi_five=(kimi_five["used"], kimi_five["time"]) if kimi_five else None,
        kimi_cycle=(kimi_cycle["used"], kimi_cycle["time"]) if kimi_cycle else None,
    )

    print(" | image=%s" % b64(mb))
    print("---")
    if claude_available:
        # Keep each Retina image near the crisp single-window Codex panel
        # height. The separator is also required on SwiftBar 2.1.1/macOS 26:
        # two adjacent image-only rows make the second attachment render from a
        # low-resolution intermediate bitmap.
        claude_five_panel = imggen.panel(
            five, None, "Claude Code", "claude_five_panel.png", single_window="five"
        )
        claude_seven_panel = imggen.panel(
            None, seven, None, "claude_seven_panel.png", single_window="seven"
        )
        print(panel_image_line(claude_five_panel))
        print("---")
        print(panel_image_line(claude_seven_panel))
    else:
        err = str(data.get("error", "")) if isinstance(data, dict) else ""
        # Only an auth failure means re-login; region/network errors are proxy issues.
        label = "重新登录" if "认证失效" in err else "无数据"
        print("Claude Code · %s | color=#8E8E93" % label)
        if err:
            print("--%s | color=#8E8E93" % err[:100])
    print("---")
    if codex_five:
        # Same split-with-separator layout as the Claude panels above.
        codex_five_panel = imggen.panel(
            codex_five, None, "Codex", "codex_five_panel.png", single_window="five"
        )
        codex_panel = imggen.panel(
            None, codex_seven, None, "codex_panel.png", single_window="seven"
        )
        print(panel_image_line(codex_five_panel))
        print("---")
        print(panel_image_line(codex_panel))
    elif codex_seven:
        codex_panel = imggen.panel(None, codex_seven, "Codex", "codex_panel.png", only_weekly=True)
        print(panel_image_line(codex_panel))
    elif codex_hidden:
        pass
    else:
        detail = (codex_data or {}).get("error", "还没有本地用量快照") if isinstance(codex_data, dict) else "还没有本地用量快照"
        print("Codex · 无数据 | color=#8E8E93")
        print("--%s | color=#8E8E93" % str(detail)[:100])
    if not codex_hidden:
        print("---")
    if kimi_five and kimi_cycle:
        # Same split-with-separator layout as the Claude panels above.
        kimi_five_panel = imggen.panel(
            kimi_five, None, "Kimi", "kimi_five_panel.png", single_window="five"
        )
        kimi_cycle_panel = imggen.panel(
            None, kimi_cycle, None, "kimi_cycle_panel.png", single_window="seven",
            seven_title="订阅周期窗口"
        )
        print(panel_image_line(kimi_five_panel))
        print("---")
        print(panel_image_line(kimi_cycle_panel))
    elif kimi_cycle:
        kimi_panel = imggen.panel(None, kimi_cycle, "Kimi", "kimi_panel.png",
                                  only_weekly=True, seven_title="订阅周期窗口")
        print(panel_image_line(kimi_panel))
    elif kimi_five:
        kimi_panel = imggen.panel(kimi_five, None, "Kimi", "kimi_panel.png",
                                  single_window="five")
        print(panel_image_line(kimi_panel))
    else:
        label = "重新登录" if "认证失效" in str(kimi_err) else "无数据"
        print("Kimi · %s | color=#8E8E93" % label)
        if kimi_err:
            print("--%s | color=#8E8E93" % str(kimi_err)[:100])
    print("---")
    try:
        wd_mode = (HERE / "watchdog_mode.txt").read_text().strip()
    except Exception:
        wd_mode = "dry"
    wd_label = "自动复活" if wd_mode == "apply" else "仅记录不复活"
    print("任务看门狗：%s" % wd_label)
    print("--自动复活断掉的任务 | bash=%s/watchdog_set_mode.sh param1=apply terminal=false refresh=true checked=%s"
          % (HERE, "true" if wd_mode == "apply" else "false"))
    print("--仅记录，不复活 | bash=%s/watchdog_set_mode.sh param1=dry terminal=false refresh=true checked=%s"
          % (HERE, "true" if wd_mode != "apply" else "false"))
    print("---")
    print("立即刷新 | bash=%s/force_refresh.sh terminal=false refresh=true" % HERE)


if __name__ == "__main__":
    main()
