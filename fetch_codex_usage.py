#!/usr/bin/env python3
"""Read Codex account rate limits from its official app-server API.

Codex writes ``token_count`` events containing ``rate_limits`` to
``~/.codex/sessions/**/*.jsonl``. This reader is local-only: it never reads auth.json,
never prints tokens, and makes no network request.
"""
import json
import os
import selectors
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
SESSIONS = CODEX_HOME / "sessions"
MAX_FILES = 40
CODEX_BIN = shutil.which("codex") or "/usr/local/bin/codex"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _read_response(proc, wanted_id, timeout=8):
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready = selector.select(max(0, deadline - time.monotonic()))
        if not ready:
            break
        line = proc.stdout.readline()
        if not line:
            break
        try:
            msg = json.loads(line)
            if msg.get("id") == wanted_id:
                return msg
        except Exception:
            continue
    raise RuntimeError("Codex app-server 响应超时")


def _normalize_bucket(bucket):
    def window(value):
        if not isinstance(value, dict):
            return None
        # Codex app-server's usedPercent is already a percentage number:
        # 1 means 1%, unlike Claude's usage API which may return 0..1.
        return {"used_percent": value.get("usedPercent"),
                "window_minutes": value.get("windowDurationMins"),
                "resets_at": value.get("resetsAt")}
    return {"limit_id": bucket.get("limitId"), "limit_name": bucket.get("limitName"),
            "primary": window(bucket.get("primary")), "secondary": window(bucket.get("secondary")),
            "credits": bucket.get("credits"), "plan_type": bucket.get("planType"),
            "rate_limit_reached_type": bucket.get("rateLimitReachedType")}


def app_server_snapshot():
    proc = subprocess.Popen([CODEX_BIN, "app-server", "--stdio"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    try:
        init = {"method": "initialize", "id": 1, "params": {"clientInfo": {
            "name": "usage-widget", "title": "Usage Widget", "version": "0.2.0"}, "capabilities": {}}}
        proc.stdin.write(json.dumps(init) + "\n"); proc.stdin.flush()
        response = _read_response(proc, 1)
        if response.get("error"):
            raise RuntimeError("Codex app-server 初始化失败")
        proc.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
        proc.stdin.write(json.dumps({"method": "account/rateLimits/read", "id": 2, "params": {}}) + "\n")
        proc.stdin.flush()
        response = _read_response(proc, 2)
        result = response.get("result") or {}
        # This is the account-level object returned by the official method.
        # Older servers exposed it as rateLimits; newer ones may additionally
        # expose named buckets, where the bucket with a weekly secondary limit
        # is the closest equivalent.
        bucket = result.get("rateLimits")
        if not isinstance(bucket, dict):
            buckets = result.get("rateLimitsByLimitId") or {}
            bucket = next((b for b in buckets.values() if isinstance(b, dict)
                           and isinstance(b.get("secondary"), dict)
                           and b["secondary"].get("windowDurationMins") == 10080), None)
            bucket = bucket or next((b for b in buckets.values() if isinstance(b, dict)), None)
        if not isinstance(bucket, dict):
            raise RuntimeError("Codex app-server 未返回限额")
        return {"observed_at": datetime.now(SHANGHAI).isoformat(),
                "source": "app-server", "rate_limits": _normalize_bucket(bucket)}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()


def _timestamp(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _candidate_files():
    try:
        files = SESSIONS.glob("**/*.jsonl")
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:MAX_FILES]
    except Exception:
        return []


def latest_snapshot():
    # Codex can rotate the limit id when the account/model bucket changes.
    # Always use the newest event so an older bucket cannot mask the active one.
    latest = None
    latest_ts = 0.0
    for path in _candidate_files():
        try:
            with path.open(errors="replace") as fh:
                for line in fh:
                    if '"rate_limits"' not in line or '"token_count"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                        payload = event.get("payload") or {}
                        limits = payload.get("rate_limits")
                        if payload.get("type") != "token_count" or not isinstance(limits, dict):
                            continue
                        ts = _timestamp(event.get("timestamp"))
                        item = {"observed_at": event.get("timestamp"), "source": "session-log",
                                "rate_limits": limits}
                        if ts > latest_ts:
                            latest, latest_ts = item, ts
                    except Exception:
                        continue
        except (OSError, PermissionError):
            continue
    if latest is None:
        raise RuntimeError("未找到 Codex 用量快照；先在 Codex 中完成一次请求")
    return latest


if __name__ == "__main__":
    try:
        try:
            result = app_server_snapshot()
        except Exception:
            result = latest_snapshot()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
