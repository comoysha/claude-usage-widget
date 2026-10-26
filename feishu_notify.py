"""飞书群通知（自定义机器人 webhook）。标准见 docs/feishu-notify-standard.md。"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional

PROJECT = "usage-widget"
WEBHOOK_ENV = "FEISHU_NOTIFY_WEBHOOK"
TIMEOUT_SECONDS = 5
LEVEL_COLORS = {"error": "red", "warning": "orange", "success": "green", "info": "blue"}
ENV_FILE = Path(__file__).resolve().parent / ".env"

# poster(url, payload) -> 飞书响应（已解析的 JSON）。测试时注入假的，不真发。
Poster = Callable[[str, Dict[str, Any]], Any]


def _webhook_from_env_file() -> str:
    """SwiftBar 拉起插件时没有 shell 环境变量，所以也读项目里的 .env（已被 git 忽略）。"""
    try:
        for line in ENV_FILE.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip() == WEBHOOK_ENV:
                return value.strip().strip("'\"")
    except OSError:
        pass
    return ""


def _http_post(url: str, payload: Dict[str, Any]) -> Any:
    # 显式不走代理：飞书请求不需要，也不能被全局代理影响。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_feishu_message(
    payload: Dict[str, Any],
    *,
    webhook_url: Optional[str] = None,
    poster: Optional[Poster] = None,
) -> bool:
    """发一条飞书消息。仅当飞书确认收下（code == 0）才返回 True；永不抛异常。"""
    url = (webhook_url or os.environ.get(WEBHOOK_ENV) or _webhook_from_env_file()).strip()
    if not url:
        return False  # 没配 webhook：静默跳过
    try:
        data = (poster or _http_post)(url, payload)
    except Exception as exc:  # noqa: BLE001 - 通知失败不能影响主流程
        # 只打异常类型：异常信息里可能带完整 webhook URL，不能进日志
        print(f"[feishu] 发送失败: {type(exc).__name__}", file=sys.stderr)
        return False
    if not isinstance(data, dict):
        print("[feishu] 响应不是 JSON 对象，视为未送达", file=sys.stderr)
        return False
    code = data.get("code", data.get("StatusCode"))
    if code != 0:
        print(f"[feishu] 飞书未收下: code={code} msg={data.get('msg')}", file=sys.stderr)
        return False
    return True


def notify_text(title: str, body: str = "", **kwargs: Any) -> bool:
    """纯文本通知。第一行自动加 [项目名] 前缀。"""
    text = f"[{PROJECT}] {title}" + (f"\n{body}" if body else "")
    return post_feishu_message({"msg_type": "text", "content": {"text": text}}, **kwargs)


def notify_card(
    title: str,
    markdown: str,
    *,
    level: str = "info",
    note: str = "",
    **kwargs: Any,
) -> bool:
    """卡片通知。level: error / warning / success / info，决定标题栏颜色。"""
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": markdown}}]
    if note:
        elements.append({"tag": "hr"})
        elements.append({"tag": "note", "elements": [{"tag": "lark_md", "content": note}]})
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": LEVEL_COLORS.get(level, "blue"),
            "title": {"tag": "plain_text", "content": f"[{PROJECT}] {title}"},
        },
        "elements": elements,
    }
    return post_feishu_message({"msg_type": "interactive", "card": card}, **kwargs)
