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
every ~20 minutes, and not at all while it idles, while the Mac sleeps or
while it's closed. So when the app's token has expired we mint our own access
token from the app's refresh token (AuthService/RefreshToken), the same call
the app makes. The refresh token returned alongside it is discarded and the
app's config is never written: Kimi keeps the old refresh token valid after a
refresh (verified 2026-10-26; the app itself fires two refreshes in parallel
at startup), so this doesn't log the app out. The minted token lives in memory
for this process only. A refresh rejected with 4xx means the login really
expired (认证失效); a network failure keeps the last good payload.
"""
import base64
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "kimi_last_fetch.json"        # last successful payload (see below)
STAMP = HERE / ".kimi_last_attempt"        # throttle touch file
DAIMON_CONFIG = (Path.home() / "Library/Application Support/kimi-desktop/"
                 "daimon-share/daimon/config.json")
USAGES_ENDPOINT = ("https://www.kimi.com/apiv2/"
                   "kimi.gateway.billing.v1.BillingService/GetUsages")
SUB_ENDPOINT = ("https://www.kimi.com/apiv2/"
                "kimi.gateway.order.v1.SubscriptionService/GetSubscription")
REFRESH_ENDPOINT = ("https://auth.kimi.com/api/"
                    "account.gateway.v1.AuthService/RefreshToken")
MIN_FETCH_S = 300
REQUEST_ATTEMPTS = 3
RETRY_DELAYS_S = (3, 10)
STATUS_MARKER = "__KIMI_WIDGET_HTTP_STATUS__"
FIVE_H_MIN = 300
CYCLE_FALLBACK_S = 30 * 86400  # subscription cycle is ~30 days; the exact
                               # span is taken from the subscription itself.
EXPIRY_MARGIN_S = 60           # treat a token this close to exp as expired


class TransientFetchError(RuntimeError):
    """A temporary network/edge response that does not prove login expiry."""


class AuthenticationError(RuntimeError):
    """An explicit HTTP authentication failure."""


class TokenStaleError(TransientFetchError):
    """No fresh token on disk and minting one failed for a transient reason."""


def _config():
    try:
        return json.loads(DAIMON_CONFIG.read_text())
    except Exception as e:
        raise RuntimeError("读不到 Kimi 桌面端配置（Kimi Work 是否登录过？）：%s" % e)


def _app_credentials():
    kimi_web = (_config().get("credentials") or {}).get("kimiWeb") or {}
    if not kimi_web.get("accessToken") and not kimi_web.get("refreshToken"):
        raise AuthenticationError("Kimi 认证失效：桌面端没有通行证（重新登录 Kimi 桌面端）")
    return kimi_web.get("accessToken"), kimi_web.get("refreshToken")


def _token_exp(tok):
    """The JWT's exp claim (epoch seconds), or None if it can't be decoded."""
    try:
        part = tok.split(".")[1]
        part += "=" * (-len(part) % 4)
        return float(json.loads(base64.urlsafe_b64decode(part))["exp"])
    except Exception:
        return None


def _is_fresh(tok, now):
    exp = _token_exp(tok) if tok else None
    return bool(tok) and (exp is None or exp - now > EXPIRY_MARGIN_S)


def _post(url, payload, headers=(), timeout=20):
    """POST JSON with curl -> (http status, body text, stderr, exit code)."""
    cmd = ["curl", "-sS", "-m", str(timeout), "-X", "POST", url,
           "-H", "Content-Type: application/json",
           "-H", "X-Msh-Platform: web",
           "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                 "AppleWebKit/537.36",
           "-w", "\n%s%%{http_code}" % STATUS_MARKER,
           "-d", json.dumps(payload)]
    for h in headers:
        cmd[-4:-4] = ["-H", h]
    p = subprocess.run(cmd, capture_output=True, text=True)
    body, marker, status_text = p.stdout.rpartition(STATUS_MARKER)
    status = int(status_text.strip() or 0) if marker else 0
    body = body.rstrip("\n") if marker else p.stdout
    return status, body, p.stderr, p.returncode


def _mint_access_token(refresh_token):
    """Exchange the app's refresh token for a fresh access token (in memory)."""
    status, body, err, rc = _post(REFRESH_ENDPOINT, {"refreshToken": refresh_token})
    if status in (400, 401, 403):
        raise AuthenticationError("Kimi 认证失效：续期被拒 (HTTP %s)" % status)
    try:
        tok = json.loads(body)["accessToken"] if status == 200 else None
    except Exception:
        tok = None
    if not tok:
        raise TokenStaleError("Kimi 续期失败 (HTTP %s: %s)" %
                              (status, body[:80] or err[:80] or "curl 退出码 %s" % rc))
    return tok


_minted = None


def _access_token(now=None):
    """A usable access token: the app's own while it's fresh, else one we mint."""
    global _minted
    now = time.time() if now is None else now
    access, refresh = _app_credentials()
    if _is_fresh(access, now):
        return access
    if _is_fresh(_minted, now):
        return _minted
    if not refresh:
        raise AuthenticationError("Kimi 认证失效：桌面端没有续期凭证（重新登录 Kimi 桌面端）")
    _minted = _mint_access_token(refresh)
    return _minted


def _curl(url, payload, timeout=20):
    last_detail = ""
    last_token = None
    for attempt in range(REQUEST_ATTEMPTS):
        # Re-read the token on every attempt: the desktop app rotates the
        # 15-minute JWT asynchronously, so a 401 often just means we caught
        # the config mid-rotation — the fresh token may already be on disk.
        token = _access_token()
        status, body, err, rc = _post(url, payload, timeout=timeout,
                                      headers=("Authorization: Bearer %s" % token,))
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
        last_detail = (body[:120] or err[:120] or "curl 退出码 %s" % rc)
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


def _parse_iso(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _roll_over(data, now=None):
    """Advance reused windows whose reset time has already passed.

    The last good payload can outlive its window when the desktop app stops
    rotating the token (idle / asleep). A window past its reset is known to be
    fresh again, so show 0% on the next window instead of the stale figure.
    Kimi's 5h windows run back to back, so the next reset is +5h; the cycle
    falls back to +CYCLE_FALLBACK_S."""
    now = time.time() if now is None else now
    for key, span in (("five_hour", FIVE_H_MIN * 60), ("cycle", CYCLE_FALLBACK_S)):
        win = data.get(key)
        reset = _parse_iso((win or {}).get("resets_at"))
        if reset is None or reset > now:
            continue
        start = reset
        while reset <= now:
            start, reset = reset, reset + span
        win["utilization"] = 0.0
        win["resets_at"] = _iso(reset)
        if "starts_at" in win:
            win["starts_at"] = _iso(start)
    return data


def _last_good():
    try:
        j = json.loads(RAW.read_text())
        return _roll_over(j) if _is_usable(j) else None
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
        # Checked before touching STAMP so a failed mint retries next tick.
        _access_token()
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
