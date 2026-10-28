#!/usr/bin/env python3
"""Fetch Cursor / Grok shared weekly usage from Cursor's dashboard API.

Cursor Premium (The X Subscription) exposes one weekly included-usage pool
shared across Cursor, Grok Bot, and the Grok apps — the same bar the editor
shows as "Weekly usage limit". There is no 5-hour window.

Auth is the desktop session JWT already stored in Cursor's VS Code state DB
(``cursorAuth/accessToken``). We open that DB read-only, never print the
token, and never write the DB.

Calls (same Connect-RPC the editor uses against ``api2.cursor.sh``):

    POST /aiserver.v1.DashboardService/GetCurrentPeriodUsage
    POST /aiserver.v1.DashboardService/GetPlanInfo

Normalized payload matches the other fetchers so ``render.find_window`` works:

    {
      "seven_day": {"utilization": <pct>, "resets_at": <iso>, "starts_at": <iso>},
      "plan": "Premium",
      "included_usage_period": "weekly",
      "observed_at": <iso>,
      "source": "api2"
    }

Throttled to one real call per MIN_FETCH_S; transient failures reuse the last
good ``cursor_last_fetch.json``. ``--force`` bypasses the throttle.
"""
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "cursor_last_fetch.json"
STAMP = HERE / ".cursor_last_attempt"
STATE_DB = (Path.home() / "Library/Application Support/Cursor/User/"
            "globalStorage/state.vscdb")
API_BASE = "https://api2.cursor.sh/aiserver.v1.DashboardService"
PERIOD_URL = API_BASE + "/GetCurrentPeriodUsage"
PLAN_URL = API_BASE + "/GetPlanInfo"
MIN_FETCH_S = 300
REQUEST_ATTEMPTS = 3
RETRY_DELAYS_S = (3, 10)
STATUS_MARKER = "__CURSOR_WIDGET_HTTP_STATUS__"
WEEKLY_MARKERS = (
    "INCLUDED_USAGE_PERIOD_WEEKLY",
    "WEEKLY",
    "2",  # numeric enum some builds still emit
)


class TransientFetchError(RuntimeError):
    """A temporary network/edge response that does not prove login expiry."""


class AuthenticationError(RuntimeError):
    """An explicit HTTP authentication failure."""


def _access_token():
    """Read Cursor's desktop access JWT (READ ONLY). Never printed."""
    if not STATE_DB.exists():
        raise AuthenticationError("Cursor 认证失效：找不到本地登录状态（先登录 Cursor）")
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise TransientFetchError("读 Cursor 登录状态失败：%s" % e)
    try:
        row = con.execute(
            "SELECT value FROM ItemTable WHERE key=?",
            ("cursorAuth/accessToken",),
        ).fetchone()
    finally:
        con.close()
    tok = (row[0] if row else None) or ""
    if not isinstance(tok, str) or not tok.strip():
        raise AuthenticationError("Cursor 认证失效：本地没有 accessToken（重新登录 Cursor）")
    return tok.strip()


def _post(url, token, payload=None, timeout=20):
    body = json.dumps(payload if payload is not None else {})
    cmd = [
        "curl", "-sS", "-m", str(timeout), "-X", "POST", url,
        "-H", "Content-Type: application/json",
        "-H", "Authorization: Bearer %s" % token,
        "-H", "Connect-Protocol-Version: 1",
        "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36",
        "-w", "\n%s%%{http_code}" % STATUS_MARKER,
        "-d", body,
    ]
    p = subprocess.run(cmd, capture_output=True, text=True)
    text, marker, status_text = p.stdout.rpartition(STATUS_MARKER)
    status = int(status_text.strip() or 0) if marker else 0
    text = text.rstrip("\n") if marker else p.stdout
    return status, text, p.stderr, p.returncode


def _curl(url, token, payload=None):
    last_detail = ""
    status = 0
    for attempt in range(REQUEST_ATTEMPTS):
        # Re-read the token each attempt in case Cursor rotated it mid-flight.
        token = _access_token()
        status, body, err, rc = _post(url, token, payload=payload)
        if status in (401, 403):
            raise AuthenticationError("Cursor 认证失效 (HTTP %s)" % status)
        if status == 200:
            try:
                return json.loads(body)
            except Exception:
                pass
        last_detail = (body[:120] or err[:120] or "curl 退出码 %s" % rc)
        if attempt + 1 < REQUEST_ATTEMPTS:
            time.sleep(RETRY_DELAYS_S[attempt])
    raise TransientFetchError("连续 %d 次失败 (HTTP %s: %s)" %
                              (REQUEST_ATTEMPTS, status, last_detail))


def _ms_to_iso(value):
    """Cursor emits epoch-ms as a JSON string (or rarely a number)."""
    if value is None or value == "":
        return None
    try:
        ms = float(value)
    except (TypeError, ValueError):
        s = str(value).strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s).astimezone(timezone.utc)\
                .isoformat().replace("+00:00", "Z")
        except Exception:
            return None
    if ms > 1e12:
        ms /= 1000.0
    return datetime.fromtimestamp(ms, timezone.utc).isoformat().replace("+00:00", "Z")


def _period_is_weekly(plan_info):
    period = (plan_info or {}).get("includedUsagePeriod")
    if period is None:
        return True  # Premium / X Subscription default; safer to show the week
    return str(period).upper() in WEEKLY_MARKERS or period == 2


def normalize(period, plan):
    plan_info = (plan or {}).get("planInfo") or {}
    usage = (period or {}).get("planUsage") or {}
    used = usage.get("totalPercentUsed")
    if used is None:
        used = usage.get("autoPercentUsed")
    try:
        used = float(used) if used is not None else None
    except (TypeError, ValueError):
        used = None

    start = _ms_to_iso(period.get("billingCycleStart"))
    # Prefer the plan's included-usage reset; fall back to the period end.
    reset = _ms_to_iso(plan_info.get("includedUsageResetsAt")) \
        or _ms_to_iso(period.get("billingCycleEnd"))

    out = {
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "api2",
        "plan": plan_info.get("planName"),
        "included_usage_period": "weekly" if _period_is_weekly(plan_info) else "other",
        "display_message": (period or {}).get("displayMessage"),
        "seven_day": {
            "utilization": used,
            "resets_at": reset,
            "starts_at": start,
        },
    }
    return out


def _is_usable(data):
    return isinstance(data, dict) and not data.get("error") and \
        (data.get("seven_day") or {}).get("utilization") is not None


def _last_good():
    try:
        j = json.loads(RAW.read_text())
        return j if _is_usable(j) else None
    except Exception:
        return None


def _recently_tried():
    try:
        return time.time() - STAMP.stat().st_mtime < MIN_FETCH_S
    except Exception:
        return False


def fetch_usage(force=False):
    if not force and _recently_tried():
        prev = _last_good()
        if prev is not None:
            return prev
    # Touch STAMP only after we can read a token, so a missing login retries
    # every tick instead of being throttled into silence.
    _access_token()
    STAMP.write_text("")
    try:
        period = _curl(PERIOD_URL, None)
        plan = _curl(PLAN_URL, None)
    except AuthenticationError:
        raise
    except TransientFetchError:
        prev = _last_good()
        if prev is not None:
            return prev
        raise
    norm = normalize(period, plan)
    if norm["seven_day"]["utilization"] is not None:
        RAW.write_text(json.dumps(norm, indent=2))
        return norm
    raise TransientFetchError("Cursor 返回里没有 weekly usage 百分比")


if __name__ == "__main__":
    try:
        data = fetch_usage(force="--force" in sys.argv[1:])
        print(json.dumps(data, ensure_ascii=False, indent=2))
    except AuthenticationError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)
