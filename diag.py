#!/usr/bin/env python3
"""Read-only keychain credential LOCATOR. Prints STRUCTURE only — never a token.

Run in your OWN terminal:  python3 ~/claude-usage-widget/diag.py
Paste the output back. It outputs no secrets: only key names, byte lengths,
expiry times, and subscription type. Never an access/refresh token value."""
import json, re, subprocess, time

SVC = "Claude Code-credentials"
now = time.time()


def kc(service):
    return subprocess.check_output(
        ["security", "find-generic-password", "-s", service, "-w"], text=True
    ).strip()


def classify(raw):
    """Token-free classification of a keychain payload."""
    if not raw:
        return "empty", "空"
    try:
        j = json.loads(raw)
    except Exception:
        # maybe a bare token / other; report only shape
        looks_jwt = raw.count(".") == 2 and len(raw) > 60
        return "nonjson", f"非JSON {len(raw)}字节{'(像裸token)' if looks_jwt else ''}"
    o = j.get("claudeAiOauth") if isinstance(j, dict) else None
    if isinstance(o, dict) and isinstance(o.get("accessToken"), str):
        d = o.get("expiresAt", 0) / 1000 - now
        state = "有效剩%.1fh" % (d / 3600) if d > 0 else "过期%.0f天" % (-d / 86400)
        tag = "oauth_ok" if d > 0 else "oauth_expired"
        return tag, f"登录凭据 | {state} | sub={o.get('subscriptionType')} | tier={o.get('rateLimitTier')}"
    top = list(j.keys())[:6] if isinstance(j, dict) else type(j).__name__
    return "json_other", f"JSON但非登录凭据 顶层键={top}"


def scan():
    winners = []
    print("=== 纯项 'Claude Code-credentials' ===")
    try:
        tag, desc = classify(kc(SVC))
        print(" ", desc)
        if tag == "oauth_ok":
            winners.append((SVC, desc))
    except Exception as e:
        print("  读取失败:", type(e).__name__)

    print("=== 后缀项 'Claude Code-credentials-<hex>' ===")
    try:
        dump = subprocess.check_output(["security", "dump-keychain"], text=True, errors="ignore")
    except Exception as e:
        print("  dump-keychain 失败:", type(e).__name__)
        dump = ""
    svcs = sorted(set(re.findall(r'"svce"<blob>="(Claude Code-credentials-[0-9a-f]+)"', dump)))
    print(f"  共 {len(svcs)} 个后缀项")
    counts = {}
    for s in svcs:
        try:
            tag, desc = classify(kc(s))
        except Exception:
            tag, desc = "readfail", "读取失败"
        counts[tag] = counts.get(tag, 0) + 1
        if tag in ("oauth_ok", "oauth_expired"):
            print(f"  [{tag}] …{s[-8:]}: {desc}")
            if tag == "oauth_ok":
                winners.append((s, desc))
    print("  分类统计:", counts)

    print("\n=== 结论 ===")
    if winners:
        print(f"  ✅ 找到 {len(winners)} 个有效未过期登录凭据:")
        for s, desc in winners:
            print(f"     service = {s}")
    else:
        print("  ❌ 没有任何有效未过期的登录凭据 → 需要在终端完整登录一次 Claude Code")


if __name__ == "__main__":
    scan()
