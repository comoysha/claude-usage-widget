#!/usr/bin/env python3
"""Multica 用量看门狗：检测因 5 小时用量窗口跑满而中断的 agent 任务，在额度恢复后复活它们。

数据源全是本地快照与非 AI 接口：
- 用量：本目录 widget 每 5 分钟刷新的 last_fetch.json / codex_last_fetch.json / kimi_last_fetch.json
- 任务与 agent：multica CLI

默认 dry-run：只把「会复活哪些任务」写进 logs/watchdog.log，不做任何改动。
加 --apply 才真正执行 multica issue rerun 复活。
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOG_DIR = BASE / "logs"
STATE_FILE = BASE / "watchdog_state.json"
LOG_FILE = LOG_DIR / "watchdog.log"

MULTICA = os.environ.get("MULTICA_BIN", "/Users/xiayue/.local/bin/multica")
# 看门狗由 launchd 在任务上下文之外运行，必须显式指定 profile；
# 且 cwd 不能落在任何 multica 任务目录里（有 daemon_task_context.json 会被拒）。
MULTICA_PROFILE = os.environ.get("MULTICA_WATCHDOG_PROFILE", "desktop-localhost-8180")

STALL_MINUTES = 25          # in_progress 任务最后活动超过这个时长视为「断了」
SNAPSHOT_MAX_AGE_MIN = 15   # widget 快照超过这个时长视为不可信
QUOTA_FULL_PCT = 95.0       # 5 小时窗口用量达到该百分比视为「满」
MAX_REVIVES_PER_6H = 2      # 同一任务 6 小时内最多复活次数（防任务本身有 bug 时死循环）
MIN_REVIVE_INTERVAL_MIN = 60

PROVIDER_SNAPSHOT = {
    "claude": BASE / "last_fetch.json",
    "codex": BASE / "codex_last_fetch.json",
    "kimi": BASE / "kimi_last_fetch.json",
}


def log(msg: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    line = f"{datetime.now().astimezone().isoformat(timespec='seconds')} {msg}"
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")
    print(line)


def run_multica(*args: str):
    r = subprocess.run(
        [MULTICA, "--profile", MULTICA_PROFILE, *args, "--output", "json"],
        capture_output=True, text=True, timeout=60, cwd=BASE,
    )
    if r.returncode != 0:
        log(f"multica {' '.join(args)} 失败: {r.stderr.strip()[:200]}")
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        log(f"multica {' '.join(args)} 输出不是 JSON")
        return None


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def quota_recovered(provider: str):
    """返回 (是否已恢复, 描述)。快照缺失/过旧/用量未知一律按未恢复处理。"""
    path = PROVIDER_SNAPSHOT.get(provider)
    if not path or not path.exists():
        return False, "无快照"
    age_min = (time.time() - path.stat().st_mtime) / 60
    if age_min > SNAPSHOT_MAX_AGE_MIN:
        return False, f"快照过旧({age_min:.0f}分钟)"
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return False, "快照不可读"

    now = datetime.now(timezone.utc)
    if provider == "claude":
        win = data.get("five_hour") or {}
        util, resets = win.get("utilization"), parse_ts(win.get("resets_at"))
    elif provider == "codex":
        win = (data.get("rate_limits") or {}).get("primary") or {}
        util = win.get("used_percent")
        ts = win.get("resets_at")
        resets = datetime.fromtimestamp(ts, timezone.utc) if ts else None
    elif provider == "kimi":
        win = data.get("five_hour") or {}
        util, resets = win.get("utilization"), parse_ts(win.get("resets_at"))
    else:
        return False, "未知供应商"

    if util is None:
        return False, "用量未知"
    if resets and now >= resets:
        return True, f"窗口已重置(上次记录{util:.0f}%)"
    if util < QUOTA_FULL_PCT:
        return True, f"用量{util:.0f}%"
    return False, f"用量{util:.0f}%仍满"


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False))


def can_revive(state, issue_id: str, now: datetime):
    cutoff = (now - timedelta(hours=6)).timestamp()
    revives = [t for t in state.get(issue_id, {}).get("revives", []) if t > cutoff]
    if len(revives) >= MAX_REVIVES_PER_6H:
        return False, f"6小时内已复活{len(revives)}次"
    if revives and (now.timestamp() - max(revives)) < MIN_REVIVE_INTERVAL_MIN * 60:
        return False, "距上次复活不足1小时"
    return True, ""


def quota_failed_run(issue: dict):
    """只复活当前负责 agent 的最近一次额度失败；活跃运行一律不碰。"""
    runs = run_multica("issue", "runs", issue["id"])
    if not isinstance(runs, list):
        return False, "执行记录不可读"
    if any(r.get("status") in {"queued", "dispatched", "running", "waiting_local_directory"} for r in runs):
        return False, "已有运行中的任务"
    if not runs:
        return False, "无执行记录"
    latest = max(runs, key=lambda r: r.get("created_at") or "")
    if latest.get("agent_id") != issue.get("assignee_id"):
        return False, "最近运行的 agent 与当前负责人不符"
    if latest.get("status") != "failed" or latest.get("failure_reason") != "agent_error.provider_quota_limit":
        return False, "最近运行并非额度耗尽失败"
    return True, ""


def answered_question_failure(issue: dict):
    """识别用户已回复、但回复触发的运行因额度失败且任务仍归用户的情况。"""
    detail = run_multica("issue", "get", issue["id"])
    if not isinstance(detail, dict) or detail.get("assignee_type") != "member":
        return None
    labels = detail.get("labels") or []
    question = next((l for l in labels if isinstance(l, dict) and l.get("name") == "等我回答"), None)
    if not question:
        return None
    runs = run_multica("issue", "runs", issue["id"])
    if not isinstance(runs, list) or not runs:
        return None
    if any(r.get("status") in {"queued", "dispatched", "running", "waiting_local_directory"} for r in runs):
        return None
    latest = max(runs, key=lambda r: r.get("created_at") or "")
    if (latest.get("status") != "failed"
            or latest.get("failure_reason") != "agent_error.provider_quota_limit"
            or latest.get("kind") != "comment"
            or not latest.get("trigger_comment_id")
            or (latest.get("attribution") or {}).get("initiator", {}).get("id") != issue.get("assignee_id")):
        return None
    return latest["agent_id"], question["id"], latest["id"]


MODE_FILE = BASE / "watchdog_mode.txt"  # apply=自动复活, dry=只记录
LOG_KEEP_DAYS = 2


def current_mode() -> str:
    """命令行 --apply/--dry 优先，否则读模式文件，缺省 dry。"""
    if "--apply" in sys.argv:
        return "apply"
    if "--dry" in sys.argv:
        return "dry"
    try:
        mode = MODE_FILE.read_text().strip()
        return "apply" if mode == "apply" else "dry"
    except OSError:
        return "dry"


def prune_log() -> None:
    """只保留最近 LOG_KEEP_DAYS 天的日志行（行首 ISO 时间戳为准）。"""
    try:
        lines = LOG_FILE.read_text().splitlines(keepends=True)
    except OSError:
        return
    cutoff = datetime.now().astimezone() - timedelta(days=LOG_KEEP_DAYS)
    kept = []
    for line in lines:
        try:
            ts = datetime.fromisoformat(line[:25])
            if ts >= cutoff:
                kept.append(line)
        except ValueError:
            kept.append(line)  # 无法解析的行（异常堆栈等）随上一条保留
    if len(kept) != len(lines):
        LOG_FILE.write_text("".join(kept))


def main():
    prune_log()
    apply_mode = current_mode() == "apply"
    now = datetime.now(timezone.utc)

    agents = run_multica("agent", "list")
    runtimes = run_multica("runtime", "list")
    if agents is None or runtimes is None:
        return
    rt_provider = {r["id"]: r.get("provider") for r in runtimes}
    agent_provider = {a["id"]: rt_provider.get(a.get("runtime_id")) for a in agents}

    issues = []
    offset = 0
    while True:
        page = run_multica(
            "issue", "list", "--status", "in_progress", "--limit", "100",
            "--offset", str(offset),
            "--fields", "id,identifier,title,status,assignee_type,assignee_id,last_activity_at",
        )
        if page is None:
            return
        issues.extend(page.get("issues", []))
        if not page.get("has_more"):
            break
        offset += len(page.get("issues", []))

    state = load_state()
    stall_cutoff = now - timedelta(minutes=STALL_MINUTES)
    candidates = 0

    for issue in issues:
        assignee_type = issue.get("assignee_type")
        if assignee_type not in {"agent", "member"}:
            continue
        last_active = parse_ts(issue.get("last_activity_at"))
        if not last_active or last_active > stall_cutoff:
            continue  # 还在活跃，正常跑着
        answered = answered_question_failure(issue) if assignee_type == "member" else None
        if assignee_type == "member" and not answered:
            continue
        agent_id = answered[0] if answered else issue.get("assignee_id")
        provider = agent_provider.get(agent_id)
        if provider not in PROVIDER_SNAPSHOT:
            continue
        candidates += 1
        stalled_min = (now - last_active).total_seconds() / 60
        ident = issue.get("identifier") or issue["id"]
        ok, why = quota_recovered(provider)
        tag = f"{ident}「{issue.get('title', '')[:20]}」agent={agent_id[:8]} provider={provider} 已停{stalled_min:.0f}分钟"
        if not ok:
            log(f"跳过 {tag}：额度未恢复（{why}）")
            continue
        allowed, reason = can_revive(state, issue["id"], now)
        if not allowed:
            log(f"跳过 {tag}：{reason}")
            continue
        if not answered:
            failed_for_quota, reason = quota_failed_run(issue)
            if not failed_for_quota:
                log(f"跳过 {tag}：{reason}")
                continue
        if apply_mode:
            if answered:
                run_multica("issue", "assign", issue["id"], "--to-id", agent_id)
                runs = run_multica("issue", "runs", issue["id"])
                result = next((r for r in runs if r.get("id") != answered[2] and
                               r.get("agent_id") == agent_id and
                               r.get("status") in {"queued", "dispatched", "running", "waiting_local_directory"}), None) if isinstance(runs, list) else None
            else:
                result = run_multica("issue", "rerun", issue["id"])
            if isinstance(result, dict) and result.get("id") and result.get("status") in {
                "queued", "dispatched", "running", "waiting_local_directory"
            }:
                log(f"已复活 {tag}，新运行={result['id']}（{why}）")
                state.setdefault(issue["id"], {}).setdefault("revives", []).append(now.timestamp())
                if answered:
                    run_multica("issue", "label", "remove", issue["id"], answered[1])
            else:
                log(f"复活未确认 {tag}：未取得新运行 ID")
        else:
            log(f"[dry-run] 会复活 {tag}（{why}）")

    save_state(state)
    if candidates == 0:
        mode = "自动复活" if apply_mode else "仅记录"
        log(f"无候选（{mode}模式）：{len(issues)} 个 in_progress 任务都在活动")


if __name__ == "__main__":
    main()
