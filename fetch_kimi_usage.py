#!/usr/bin/env python3
"""Fetch Kimi Code usage (5-hour window + subscription-cycle quota) from Kimi's
official web API.

The Kimi desktop app (kimi-desktop) keeps a kimi.com web session in its daimon
config (``credentials.kimiWeb.accessToken``, a 15-minute JWT). We reuse it
READ ONLY to call the same Connect-RPC endpoints the kimi.com settings page
and the Kimi Code console use:

    POST https://www.kimi.com/apiv2/kimi.gateway.billing.v1.BillingService/GetUsages
    POST https://www.kimi.com/apiv2/kimi.gateway.order.v1.SubscriptionService/GetSubscription

GetUsages (scope FEATURE_CODING) returns percent-normalized quotas
(limit "100" = 100%):

    usages[0].limits[0] -> {"window": {"duration": 300, "timeUnit": "MINUTE"},
                            "detail": {"limit", "remaining", "resetTime"}}  # 5h
    totalQuota          -> monthly shared pool used% (the console's 总使用量)

The Kimi console shows exactly two bars: 5 小时用量 and 总使用量 (the
subscription-cycle quota, ~30 days, reset at nextBillingTime), so that is
what we report as five_hour / cycle. The 7-day window only exists for legacy
plans and is intentionally not mapped.

The access token is read fresh on every real call and never printed or cached.
Like fetch_usage.py: throttled to one real call per MIN_FETCH_S; transient
failures reuse the last good payload (kimi_last_fetch.json keeps the previous
success until the next real call writes fresh data or an {"error": ...} tick).

Token freshness: the JWT lives 15 minutes but the desktop app only rotates it
every ~20 minutes (and not at all while the Mac sleeps or the app is closed),
so the token on disk is routinely expired for a few minutes per cycle. An
expired token is therefore NOT a login failure: we skip the call and keep the
last good payload. Only when the app is running and the same expired token has
sat on disk for STALE_ALARM_S of continuous awake time (the app missed at
least one rotation) do we report 认证失效. .kimi_token_state.json tracks that,
storing only a hash of the token.
"""
import base64
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "kimi_last_fetch.json"        # last successful payload (see below)
STAMP = HERE / ".kimi_last_attempt"        # throttle touch file
TOKEN_STATE = HERE / ".kimi_token_state.json"  # expired-token watch (hash only)
DAIMON_CONFIG = (Path.home() / "Library/Application Support/kimi-desktop/"
                 "daimon-share/daimon/config.json")
USAGES_ENDPOINT = ("https://www.kimi.com/apiv2/"
                   "kimi.gateway.billing.v1.BillingService/GetUsages")
SUB_ENDPOINT = ("https://www.kimi.com/apiv2/"
                "kimi.gateway.order.v1.SubscriptionService/GetSubscription")
MIN_FETCH_S = 300
REQUEST_ATTEMPTS = 3
RETRY_DELAYS_S = (3, 10)
STATUS_MARKER = "__KIMI_WIDGET_HTTP_STATUS__"
FIVE_H_MIN = 300
CYCLE_FALLBACK_S = 30 * 86400  # subscription cycle is ~30 days; the exact
                               # span is taken from the subscription itself.
EXPIRY_MARGIN_S = 60           # treat a token this close to exp as expired
STALE_ALARM_S = 30 * 60        # app rotates every ~20 min; 30 min = missed one
SLEEP_GAP_S = 15 * 60          # a gap this long between checks = Mac slept
APP_PROCESS = "Kimi"


class TransientFetchError(RuntimeError):
    """A temporary network/edge response that does not prove login expiry."""


class AuthenticationError(RuntimeError):
    """An explicit HTTP authentication failure."""


class TokenStaleError(TransientFetchError):
    """The token on disk expired and the desktop app hasn't rotated it yet."""


def _config():
    try:
        return json.loads(DAIMON_CONFIG.read_text())
    except Exception as e:
        raise RuntimeError("读不到 Kimi 桌面端配置（Kimi Work 是否登录过？）：%s" % e)


def _access_token():
    tok = (_config().get("credentials") or {}).get("kimiWeb", {}).get("accessToken")
    if not tok:
        raise AuthenticationError("Kimi 认证失效：桌面端没有通行证（重新登录 Kimi 桌面端）")
    return tok


def _token_exp(tok):
    """The JWT's exp claim (epoch seconds), or None if it can't be decoded."""
    try:
        part = tok.split(".")[1]
        part += "=" * (-len(part) % 4)
        return float(json.loads(base64.urlsafe_b64decode(part))["exp"])
    except Exception:
        return None


def _app_running():
    try:
        return subprocess.run(["pgrep", "-x", APP_PROCESS],
                              capture_output=True).returncode == 0
    except Exception:
        return True  # can't tell: assume running so a real logout still surfaces


def _expired_for_awake_s(tok, now):
    """How long (awake seconds) this same expired token has been seen on disk.

    Resets when the token changes or when the gap since the previous check is
    long enough that the Mac must have been asleep (the app can't rotate then,
    so that time must not count against it)."""
    digest = hashlib.sha256(tok.encode()).hexdigest()[:16]
    try:
        st = json.loads(TOKEN_STATE.read_text())
    except Exception:
        st = {}
    if st.get("token") != digest or now - st.get("last_seen", 0) > SLEEP_GAP_S:
        st = {"token": digest, "first_seen": now}
    st["last_seen"] = now
    try:
        TOKEN_STATE.write_text(json.dumps(st))
    except Exception:
        pass
    return now - st["first_seen"]


def _clear_token_state():
    try:
        TOKEN_STATE.unlink()
    except Exception:
        pass


def _check_token_fresh(now=None):
    """Raise TokenStaleError while the app hasn't rotated an expired token yet,
    AuthenticationError once the app has evidently failed to rotate it."""
    now = time.time() if now is None else now
    tok = _access_token()
    exp = _token_exp(tok)
    if exp is None or exp - now > EXPIRY_MARGIN_S:
        _clear_token_state()
        return
    stale_s = _expired_for_awake_s(tok, now)
    if stale_s >= STALE_ALARM_S and _app_running():
        raise AuthenticationError(
            "Kimi 认证失效：Kimi App 已 %d 分钟没续上通行证" % (stale_s // 60))
    raise TokenStaleError("等待 Kimi App 刷新通行证")


def _curl(url, payload, timeout=20):
    last_detail = ""
    last_token = None
    for attempt in range(REQUEST_ATTEMPTS):
        # Re-read the token on every attempt: the desktop app rotates the
        # 15-minute JWT asynchronously, so a 401 often just means we caught
        # the config mid-rotation — the fresh token may already be on disk.
        token = _access_token()
        p = subprocess.run(
            ["curl", "-sS", "-m", str(timeout), "-X", "POST", url,
             "-H", "Authorization: Bearer %s" % token,
             "-H", "Content-Type: application/json",
             "-H", "X-Msh-Platform: web",
             "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36",
             "-w", "\n%s%%{http_code}" % STATUS_MARKER,
             "-d", json.dumps(payload)],
            capture_output=True, text=True,
        )
        body, marker, status_text = p.stdout.rpartition(STATUS_MARKER)
        status = int(status_text.strip() or 0) if marker else 0
        body = body.rstrip("\n") if marker else p.stdout
        if status == 401:
            # Only a 401 with an UNCHANGED token proves login expiry. If the
            # desktop has since written a new token, retry with it first.
            if token != last_token and attempt + 1 < REQUEST_ATTEMPTS:
                last_detail = "401, token rotated, retrying with fresh token"
                last_token = token
                time.sleep(RETRY_DELAYS_S[attempt])
                continue
            raise AuthenticationError("Kimi 认证失效 (HTTP 401)")
        if status == 200:
            try:
                return json.loads(body)
            except Exception:
                pass
        last_detail = (body[:120] or p.stderr[:120] or "curl 退出码 %s" % p.returncode)
        if attempt + 1 < REQUEST_ATTEMPTS:
            time.sleep(RETRY_DELAYS_S[attempt])
    raise TransientFetchError("连续 %d 次失败 (HTTP %s: %s)" %
                              (REQUEST_ATTEMPTS, status, last_detail))


def _quota_used_pct(detail):
    """Kimi reports percent-normalized quotas: limit '100' = 100%.

    The API is protobuf-JSON, which omits zero-valued fields: an exhausted
    window comes back as {"limit": "100", "used": "100"} with no "remaining",
    and an untouched one as {"limit": "100", "remaining": "100"} with no
    "used". Prefer "used"; otherwise derive it from "remaining" (missing = 0)."""
    if not isinstance(detail, dict):
        return None
    try:
        limit = float(detail.get("limit"))
        if detail.get("used") is not None:
            used = float(detail.get("used"))
        else:
            used = limit - float(detail.get("remaining") or 0)
    except (TypeError, ValueError):
        return None
    if limit <= 0:
        return None
    return max(0.0, min(100.0, used / limit * 100.0))


def _window_from_limit(lim):
    """A limits[] entry -> {used_pct, resets_at} for a known window size."""
    if not isinstance(lim, dict):
        return None
    win = lim.get("window") or {}
    try:
        minutes = int(win.get("duration"))
    except (TypeError, ValueError):
        return None
    if win.get("timeUnit") not in (None, "TIME_UNIT_MINUTE"):
        return None
    used = _quota_used_pct(lim.get("detail"))
    if used is None:
        return None
    reset = ((lim.get("detail") or {}).get("resetTime"))
    return {minutes: (used, reset)}


def normalize(data, sub):
    """Split the FEATURE_CODING usage into five_hour / cycle.

    five_hour = rolling 5-hour window (usages[0].limits[300min]).
    cycle     = subscription-cycle quota (the console's 总使用量): used% comes
                from totalQuota; the window starts at the subscription's
                currentStartTime and resets at nextBillingTime (the console
                shows exactly this reset time).

    utilization is in percentage points; *resets_at* / *starts_at* are ISO
    strings (epoch 0 when unknown)."""
    out = {"five_hour": None, "cycle": None, "plan": None,
           "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    if not isinstance(data, dict):
        return out
    sub = sub if isinstance(sub, dict) else {}
    purchase = sub.get("purchaseSubscription") or sub.get("subscription") or {}
    goods = purchase.get("goods") or {}
    if goods.get("title"):
        out["plan"] = goods.get("title")
    cycle_start = purchase.get("currentStartTime")
    cycle_reset = purchase.get("nextBillingTime") or purchase.get("currentEndTime")

    usages = data.get("usages")
    usage = None
    for u in usages if isinstance(usages, list) else []:
        if isinstance(u, dict) and u.get("scope") == "FEATURE_CODING":
            usage = u
            break
    if usage is None and isinstance(usages, list) and usages:
        usage = usages[0] if isinstance(usages[0], dict) else None
    if usage is None:
        return out
    five_used = five_reset = None
    for lim in usage.get("limits") or []:
        w = _window_from_limit(lim)
        if w and FIVE_H_MIN in w:
            five_used, five_reset = w[FIVE_H_MIN]
    out["five_hour"] = {"utilization": five_used, "resets_at": five_reset}
    total = data.get("totalQuota")
    cycle_used = _quota_used_pct(total)
    if cycle_used is not None:
        # Prefer the billing-reported reset; fall back to the subscription's
        # next billing time (GetUsages leaves resetTime empty on fresh cycles).
        cycle_reset = (total.get("resetTime") if isinstance(total, dict) else None) \
            or cycle_reset
        out["cycle"] = {"utilization": cycle_used,
                        "resets_at": cycle_reset, "starts_at": cycle_start}
    return out


def _is_usable(data):
    return isinstance(data, dict) and not data.get("error") and \
        (data.get("five_hour") or {}).get("utilization") is not None


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
    try:
        # Checked before touching STAMP so the next tick retries as soon as
        # the app has written a fresh token.
        _check_token_fresh()
    except TokenStaleError:
        prev = _last_good()
        if prev is not None:
            return prev
        raise
    STAMP.write_text("")
    try:
        j = _curl(USAGES_ENDPOINT, {"scope": ["FEATURE_CODING"]})
        sub = _curl(SUB_ENDPOINT, {})
    except AuthenticationError:
        raise
    except TransientFetchError:
        prev = _last_good()
        if prev is not None:
            return prev
        raise
    norm = normalize(j, sub)
    if norm["five_hour"]["utilization"] is not None:
        RAW.write_text(json.dumps(norm, indent=2))
        return norm
    raise TransientFetchError("Kimi 返回里没有 FEATURE_CODING 用量窗口")


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
