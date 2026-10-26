"""额度打满时发飞书推送。

由 render.py 每个刷新周期调用一次。任一订阅的任一窗口（5 小时 / 7 天 / 30 天）
用量达到 100%，就合并成一张卡片推送；同一窗口在同一个重置周期内只推一次，
窗口重置后（用量回落）发一张“已恢复”卡片并重新待命。只有飞书确认收下才记“已通知”，
所以网络抖动或还没配 webhook 时，下一轮会自动补发。
"""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feishu_notify

HERE = Path(__file__).resolve().parent
STATE = HERE / "alert_state.json"
THRESHOLD = 100.0
# 同一窗口的重置时间每次取到的值会有秒级抖动；相差不到这个值就认为是同一个周期。
SAME_CYCLE_S = 3600
TZ = timezone(timedelta(hours=8), "Asia/Shanghai")


def _load_state():
    try:
        data = json.loads(STATE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state):
    try:
        STATE.write_text(json.dumps(state, indent=2))
    except OSError:
        pass


def _fmt_time(epoch):
    if not epoch:
        return "未知"
    return datetime.fromtimestamp(epoch, TZ).strftime("%Y-%m-%d %H:%M (Asia/Shanghai)")


def check(items, *, now=None, dry_run=False, poster=None, state_path=None):
    """items: [{"key", "name", "window", "used", "reset"}]。返回本次是否发出了通知。"""
    global STATE
    if state_path is not None:
        STATE = Path(state_path)
    now = now or time.time()
    state = _load_state()
    fired = []
    recovered = []
    for it in items:
        key, used, reset = it["key"], it.get("used"), it.get("reset")
        if used is None:
            continue  # 取不到数据：不判断，也不动状态
        if used < THRESHOLD:
            if key in state:  # 回落 = 窗口已重置，要发“已恢复”
                recovered.append(it)
            continue
        prev = state.get(key)
        if prev and (not reset or abs(prev.get("reset", 0) - reset) < SAME_CYCLE_S):
            continue  # 这个周期已经推过
        fired.append(it)
    sent = False
    if fired:
        sent = _send_exhausted(fired, state, now, dry_run, poster)
    if recovered:
        sent = _send_recovered(recovered, state, dry_run, poster) or sent
    return sent


def _send_exhausted(fired, state, now, dry_run, poster):
    lines = []
    for it in fired:
        lines.append("**%s · %s** 已用满 100%%\n重置时间：%s" % (
            it["name"], it["window"], _fmt_time(it.get("reset"))))
    markdown = "\n\n".join(lines)
    title = "额度用满：" + "、".join("%s %s" % (i["name"], i["window"]) for i in fired)
    note = "用满期间调用会被限流；到重置时间自动恢复。"
    if dry_run:
        print(title)
        print(markdown)
        return False
    ok = feishu_notify.notify_card(title, markdown, level="error", note=note, poster=poster)
    if ok:
        for it in fired:
            state[it["key"]] = {"reset": it.get("reset") or 0, "notified_at": now}
        _save_state(state)
    return ok


def _send_recovered(recovered, state, dry_run, poster):
    lines = ["**%s · %s** 额度已恢复（当前已用 %.0f%%）" % (
        it["name"], it["window"], it["used"]) for it in recovered]
    markdown = "\n\n".join(lines)
    title = "额度已恢复：" + "、".join("%s %s" % (i["name"], i["window"]) for i in recovered)
    if dry_run:
        print(title)
        print(markdown)
        return False
    ok = feishu_notify.notify_card(title, markdown, level="success", poster=poster)
    if ok:  # 送达后才清除标记，失败则下一轮重试
        for it in recovered:
            state.pop(it["key"], None)
        _save_state(state)
    return ok
