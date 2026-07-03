#!/usr/bin/env python3
"""Log the end-of-window usage percentage for every 7-day and 5-hour window.

Each usage window is identified by its ``resets_at`` timestamp. We can't reliably
poll at the exact instant a window ends (the /usage endpoint is throttled), so we
track the *latest observed* utilization for the currently-open window and, the moment
we see ``resets_at`` roll over to a new value, treat the previous window as closed and
append its final observed utilization to the log.

Two logs are produced from the same events:

* ``window_log.jsonl`` — the raw machine record, one JSON object per completed window
  (source of truth).
* ``window_log.md`` — a human-friendly Markdown view, rendered fresh from the JSONL on
  every update. Shows, per window: the date, the window's start and end (首尾), and the
  endpoint utilization %.

Idempotent and side-effect-safe: call ``record(data)`` on every fresh usage payload.
Missing/partial data is ignored rather than corrupting the log.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "window_state.json"
LOG = HERE / "window_log.jsonl"
MD = HERE / "window_log.md"

# The window kinds we track: block names in the payload + span + display label.
KINDS = {
    "five_hour": {"names": ("five_hour", "fiveHour"), "span": 5 * 3600, "label": "5 小时窗口"},
    "seven_day": {"names": ("seven_day", "sevenDay"), "span": 7 * 86400, "label": "7 天窗口"},
}


def _iso(epoch=None):
    ts = time.time() if epoch is None else epoch
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _pct(v):
    if v is None:
        return None
    v = float(v)
    return v * 100 if v <= 1.0 else v


def _extract(data, names):
    """Return (utilization_pct, resets_at_str) for a window block, or (None, None)."""
    if not isinstance(data, dict):
        return None, None
    blk = None
    for n in names:
        if isinstance(data.get(n), dict):
            blk = data[n]
            break
    if blk is None:
        return None, None
    used = None
    for k in ("utilization", "used_pct", "usedPercent", "percent_used", "usage"):
        if k in blk:
            used = _pct(blk[k])
            break
    reset = None
    for k in ("resets_at", "resetsAt", "reset_at", "resetAt"):
        if k in blk:
            reset = blk[k]
            break
    return used, (str(reset) if reset is not None else None)


def _load_state():
    try:
        s = json.loads(STATE.read_text())
        return s if isinstance(s, dict) else {}
    except Exception:
        return {}


def _save_state(state):
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE)


def _append_log(entry):
    with LOG.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _parse_iso(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _fmt_local(dt):
    """ISO datetime (UTC-aware) → local 'YYYY-MM-DD HH:MM'. '—' if unparseable."""
    if dt is None:
        return "—"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def render_md():
    """Regenerate window_log.md from window_log.jsonl (raw JSONL stays authoritative)."""
    rows = {k: [] for k in KINDS}
    try:
        for line in LOG.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("kind") in rows:
                rows[e["kind"]].append(e)
    except FileNotFoundError:
        pass

    out = ["# 用量窗口日志", "",
           "每个窗口终点（重置时刻）的用量百分比。时间为本地时区。", ""]
    for kind, cfg in KINDS.items():
        out.append(f"## {cfg['label']}")
        out.append("")
        out.append("| 日期 | 窗口起点 | 窗口终点 | 终点用量% |")
        out.append("| --- | --- | --- | --- |")
        entries = sorted(rows[kind], key=lambda e: str(e.get("resets_at")))
        if not entries:
            out.append("| — | — | — | — |")
        for e in entries:
            end = _parse_iso(e.get("resets_at"))
            start = None
            if end is not None:
                from datetime import timedelta
                start = end - timedelta(seconds=cfg["span"])
            date = end.astimezone().strftime("%Y-%m-%d") if end else "—"
            util = e.get("end_util")
            util_s = f"{util:g}%" if isinstance(util, (int, float)) else "—"
            out.append(f"| {date} | {_fmt_local(start)} | {_fmt_local(end)} | {util_s} |")
        out.append("")
    MD.write_text("\n".join(out))


def _seal(kind, prev, now_iso):
    """Build a completed-window log entry from the last observation of that window."""
    return {
        "kind": kind,
        "resets_at": prev.get("resets_at"),
        "end_util": prev.get("last_util"),
        "end_util_seen_at": prev.get("last_seen"),
        "logged_at": now_iso,
    }


def record(data):
    """Update per-window state from a fresh usage payload; append a log line for any
    window that has just ended. Returns the list of completed entries written.

    A window is sealed the MOMENT its ``resets_at`` is in the past — we no longer wait
    to observe the *next* window's resets_at. That matters because a fresh, unused 5h
    window reports ``resets_at: null`` (0% usage); under the old rollover-only logic the
    just-finished window would then sit un-logged indefinitely (its final % stranded in
    window_state.json) until the user happened to drive usage in the new window."""
    state = _load_state()
    written = []
    now_iso = _iso()
    now_epoch = time.time()

    for kind, cfg in KINDS.items():
        used, reset = _extract(data, cfg["names"])
        reset_dt = _parse_iso(reset) if reset else None
        reset_epoch = reset_dt.timestamp() if reset_dt else None

        prev = state.get(kind)
        prev_epoch = prev.get("resets_epoch") if prev else None
        if prev_epoch is None and prev and prev.get("resets_at"):
            pdt = _parse_iso(prev["resets_at"])  # tolerate old state files w/o epoch
            prev_epoch = pdt.timestamp() if pdt else None

        # 1) Clock-based seal: if the tracked window has already ended and we haven't
        #    logged it, append its final observed utilization NOW — even when this tick
        #    carries no (or a null) resets_at for the new window.
        if prev and not prev.get("logged") and prev_epoch is not None and now_epoch >= prev_epoch:
            entry = _seal(kind, prev, now_iso)
            _append_log(entry)
            written.append(entry)
            prev["logged"] = True
            state[kind] = prev

        # 2) Fold in this tick's observation.
        if reset_epoch is None:
            continue  # idle window (resets_at null): nothing new to track this tick

        # The API jitters resets_at by ~1-2s within the SAME window; only a shift beyond
        # half the span counts as a genuinely new window.
        same_window = (prev_epoch is not None
                       and abs(reset_epoch - prev_epoch) <= cfg["span"] / 2)

        if same_window:
            if prev.get("logged"):
                continue  # a just-sealed window still lingering in the payload; ignore
            # Still the open window: refresh utilization, keep the FIRST-seen resets_at.
            prev["last_util"] = used
            prev["last_seen"] = now_iso
            state[kind] = prev
        else:
            # A genuinely new window (rolled over, or the first observation ever).
            # Safety net: seal the old window if step 1 somehow didn't (should be rare).
            if prev and not prev.get("logged") and prev_epoch is not None:
                entry = _seal(kind, prev, now_iso)
                _append_log(entry)
                written.append(entry)
            state[kind] = {
                "resets_at": reset,
                "resets_epoch": reset_epoch,
                "last_util": used,
                "last_seen": now_iso,
            }

    _save_state(state)
    if written:
        render_md()  # refresh the human-friendly view whenever a window closes
    return written


if __name__ == "__main__":
    import sys
    src = HERE / "usage_raw.json"
    try:
        data = json.loads(src.read_text())
    except Exception as e:
        print(f"cannot read {src}: {e}", file=sys.stderr)
        sys.exit(1)
    done = record(data)
    render_md()  # always keep the MD view in sync when run manually
    if done:
        for e in done:
            print(f"logged {e['kind']} end={e['end_util']}% (reset {e['resets_at']})")
    else:
        print("state updated; no window rolled over this tick")
