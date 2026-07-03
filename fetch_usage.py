#!/usr/bin/env python3
"""Fetch Claude usage the way the Desktop app does — via the claude.ai web session.

The Claude *Desktop* app is an Electron (claude.ai) web app. It authenticates with a
``sessionKey`` cookie, NOT the Claude Code CLI's keychain OAuth token — they are two
entirely separate credential systems (that's why the CLI can't see Desktop's chats).

We read Desktop's cookie because the user lives in Desktop, so that cookie is kept
continuously fresh. The old approach read the CLI's keychain OAuth token, which went
stale whenever the CLI wasn't used and then hammered a rate-limited refresh endpoint
trying (and always failing) to renew it — blanking the widget for long stretches.

Pipeline (READ ONLY — we never write the cookie store or the keychain):
  1. Decrypt the ``sessionKey`` cookie from Desktop's Chromium cookie store, using the
     "Claude Safe Storage" keychain key (Chromium v10: AES-128-CBC, PBKDF2 key from
     saltysalt/1003 iterations, IV = 16 spaces; newer Chromium prepends a 32-byte
     SHA256(domain) to the plaintext, which we strip).
  2. Discover the chat/subscription org uuid via /api/organizations (cached).
  3. GET https://claude.ai/api/organizations/{org}/usage  (Cookie + User-Agent).
     NB: claude.ai returns an EMPTY body without a browser-like User-Agent.

Caches usage to usage_raw.json and feeds window rollovers to window_log. Throttled to
one real call per MIN_FETCH_S; between calls it serves the last good payload.
"""
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "usage_raw.json"
STAMP = HERE / ".last_attempt"     # touch-file to throttle real API calls
ORG_CACHE = HERE / ".org.json"     # cached chat-org uuid (avoids an extra call each tick)
COOKIES_DB = Path.home() / "Library/Application Support/Claude/Cookies"
SAFE_STORAGE_SVC = "Claude Safe Storage"  # macOS keychain item holding Chromium's cookie key
BASE = "https://claude.ai/api"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Claude/1.0 Chrome/126 Electron/31 Safari/537.36")
MIN_FETCH_S = 300  # min seconds between real /usage calls


def _safe_storage_key():
    """Chromium's cookie-encryption password from the keychain (READ ONLY)."""
    return subprocess.check_output(
        ["security", "find-generic-password", "-s", SAFE_STORAGE_SVC, "-w"],
        text=True,
    ).strip()


def get_session_key():
    """Decrypt and return the live claude.ai ``sessionKey`` from Desktop's cookie store."""
    key = _safe_storage_key()
    # Open the cookie DB immutable so a running Desktop (which holds a write lock)
    # doesn't block our read; a slightly stale snapshot is fine for a cookie.
    con = sqlite3.connect(f"file:{COOKIES_DB}?immutable=1", uri=True)
    try:
        row = con.execute(
            "select encrypted_value from cookies "
            "where name='sessionKey' and host_key like '%claude.ai%'"
        ).fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        raise RuntimeError("找不到 Desktop 的 sessionKey cookie（Claude Desktop 是否已登录？）")
    enc = row[0]
    dk = hashlib.pbkdf2_hmac("sha1", key.encode(), b"saltysalt", 1003, 16)
    p = subprocess.run(
        ["openssl", "enc", "-d", "-aes-128-cbc", "-K", dk.hex(), "-iv", "20" * 16, "-nopad"],
        input=enc[3:], capture_output=True,  # enc[3:] drops the 'v10' prefix
    )
    pt = p.stdout
    if not pt:
        raise RuntimeError("cookie 解密失败（Claude Safe Storage 密钥不匹配？）")
    pt = pt[:-pt[-1]]  # strip PKCS7 padding
    # Newer Chromium prepends 32 bytes of SHA256(domain); try stripped form first.
    for cand in (pt[32:], pt):
        try:
            s = cand.decode()
        except Exception:
            continue
        if s.startswith("sk-ant"):
            return s
    raise RuntimeError("解密结果不是有效的 sessionKey")


def curl_json(url, cookie, timeout=25):
    p = subprocess.run(
        ["curl", "-sS", "-m", str(timeout),
         "-H", f"Cookie: sessionKey={cookie}",
         "-H", f"User-Agent: {UA}",
         "-H", "Accept: application/json", url],
        capture_output=True, text=True,
    )
    try:
        return json.loads(p.stdout)
    except Exception:
        raise RuntimeError(f"非 JSON 响应 ({p.stdout[:120] or p.stderr[:120]})")


def get_org(cookie):
    """Return the chat/subscription org uuid, discovering + caching it if needed."""
    try:
        c = json.loads(ORG_CACHE.read_text())
        if c.get("uuid"):
            return c["uuid"]
    except Exception:
        pass
    orgs = curl_json(f"{BASE}/organizations", cookie)
    pick = None
    for o in orgs if isinstance(orgs, list) else []:
        caps = o.get("capabilities") or []
        if any(c in caps for c in ("chat", "claude_max", "claude_pro")):
            pick = o.get("uuid")
            break
    if not pick and isinstance(orgs, list) and orgs:
        pick = orgs[0].get("uuid")
    if not pick:
        raise RuntimeError("找不到聊天组织（capabilities 里没有 chat）")
    ORG_CACHE.write_text(json.dumps({"uuid": pick}))
    return pick


def call_usage(cookie, org):
    return curl_json(f"{BASE}/organizations/{org}/usage", cookie)


def recently_tried():
    """True if we hit the endpoint within MIN_FETCH_S (throttle real calls)."""
    try:
        return time.time() - STAMP.stat().st_mtime < MIN_FETCH_S
    except Exception:
        return False


def _last_good():
    """Last successful usage payload, regardless of age (graceful degradation)."""
    try:
        j = json.loads(RAW.read_text())
        return j if isinstance(j, dict) and "five_hour" in j and not j.get("error") else None
    except Exception:
        return None


def fetch_usage(force=False):
    # "立即刷新" passes force=True so a user-initiated refresh always makes a real
    # API call — the MIN_FETCH_S throttle only guards the automatic 5-minute ticks.
    if not force and recently_tried():
        prev = _last_good()
        if prev is not None:
            return prev  # throttled: reuse last good data, no request
    STAMP.write_text("")  # mark this attempt (bumps mtime) before we call
    cookie = get_session_key()
    org = get_org(cookie)
    j = call_usage(cookie, org)
    # cache ONLY on a fresh success — restarts the throttle window
    if isinstance(j, dict) and "five_hour" in j and not j.get("error"):
        RAW.write_text(json.dumps(j, indent=2))
        # Track window rollovers / log end-of-window utilization. Never let a logging
        # hiccup break the usage fetch itself.
        try:
            import window_log
            window_log.record(j)
        except Exception:
            pass
    return j


if __name__ == "__main__":
    force = "--force" in sys.argv[1:]
    try:
        data = fetch_usage(force=force)
        if isinstance(data, dict) and "five_hour" in data and not data.get("error"):
            print(json.dumps(data, indent=2))
        else:
            prev = _last_good()
            if prev is not None:
                print(json.dumps(prev, indent=2))
            else:
                print(json.dumps(data if isinstance(data, dict) else {"error": str(data)}))
                sys.exit(1)
    except Exception as e:
        prev = _last_good()
        if prev is not None:
            print(json.dumps(prev, indent=2))
        else:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
