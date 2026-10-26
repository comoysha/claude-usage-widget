# 飞书群通知标准（跨项目统一）

> **用法**：新项目需要「关键节点发通知」时，把这篇文档直接发给 AI 助手，说一句
> 「按这篇标准接入飞书通知」即可。本文自包含，不需要再参考其他项目的代码。
>
> 本标准提炼自 subtitleops_hub 的线上实现（告警、日报、健康检查、审批提醒都走它）。

---

## 0. 给 AI 的任务清单

接入时按顺序做完这 7 件事：

0. 把本文存到项目的 `docs/feishu-notify-standard.md`，以后改通知逻辑时有据可查。
1. 在 `.env.example` 加 `FEISHU_NOTIFY_WEBHOOK=`（带注释），真实值只写进 `.env`，**向用户要**。
2. 把第 6 节的参考实现放进项目（Python 或 TypeScript 选一个，与项目技术栈一致），改掉 `PROJECT` 常量。
3. 业务代码只调用 `notify_text` / `notify_card`，**不要**在业务代码里直接拼 HTTP 请求。
4. 在第 3 节列出的节点上接通知；高频事件必须按第 5 节做聚合 / 冷却。
5. 写测试：注入假的 `poster`，不真发（见第 7 节）。
6. 在 README 的环境变量说明里补一行 `FEISHU_NOTIFY_WEBHOOK`。

---

## 1. 通道

- 用**飞书群自定义机器人 webhook**：往一个 URL POST JSON 就能在群里发消息，不需要建飞书应用、不需要 token 换取。
- URL 形如 `https://open.feishu.cn/open-apis/bot/v2/hook/<一串 token>`。
- **所有项目发到同一个群、用同一个 webhook**。所以消息里必须带项目名（见第 4 节），否则群里分不清是谁发的。
- 这个机器人**没有开启签名校验**（现有实现都不带签名）。不要自作主张加签名逻辑。

## 2. 配置

| 键名 | 必填 | 说明 |
| --- | --- | --- |
| `FEISHU_NOTIFY_WEBHOOK` | 否 | 通知群的 webhook URL。**留空 = 不发通知**，程序照常运行。 |
| `FEISHU_NOTIFY_WEBHOOK_<用途>` | 否 | 个别消息要发到别的群时才加（例如含邮箱 / 手机号等个人信息的审批提醒，应发到成员更少的群）。空则回落到 `FEISHU_NOTIFY_WEBHOOK`。 |

规则：

- **webhook URL 等同密钥**：谁拿到谁就能往群里发消息。只放 `.env`，禁止写进代码、提交进 Git、打印进日志（包括异常信息——很多 HTTP 库的报错会带上完整 URL）。
- 新项目一律用 `FEISHU_NOTIFY_WEBHOOK` 这个键名，不要加项目前缀。（subtitleops_hub 里叫 `SUBTITLEOPS_FEISHU_RISK_WEBHOOK`，是历史命名，值是同一个。）
- **本地开发 / 测试环境的 `.env` 不要填真实值**，避免调试时往真实群里刷消息。只有线上（或明确要验证通知时）才填。

`.env.example` 里这样写：

```bash
# 飞书群通知（自定义机器人 webhook）。留空 = 不发通知。标准见 docs/feishu-notify-standard.md
FEISHU_NOTIFY_WEBHOOK=
```

## 3. 什么时候发

通知是给人看的，群里消息多了就没人看。只在下面几类节点发：

| 类别 | 典型场景 | 级别 |
| --- | --- | --- |
| **告警** | 定时任务失败、外部依赖挂了、磁盘 / 额度快满 | `error` / `warning` |
| **恢复** | 之前告过警的问题好了（发过告警就必须发恢复） | `success` |
| **待办** | 需要人来处理：有新的申请要审批、需要人工确认 | `info` |
| **汇总** | 日报 / 批量任务跑完的结果统计 | 有失败 `error`，全绿 `success` |
| **完成** | 耗时很长的一次性任务结束（成功也发，免得人盯着） | `success` |

不要发的：每条成功的常规任务、调试信息、每次重试。

## 4. 消息格式

### 4.1 通用规则

- **第一行 / 标题必须以 `[项目名]` 开头**，后面跟事件，例如 `[subtitleops] R2 备份失败`。
- **时间带时区**：`2026-10-01 06:30:00 (Asia/Shanghai)`。服务器多半是 UTC，不带时区会看错。
- **告警要写下一步**：告诉人该去看哪、跑什么命令，而不只是「出错了」。
- **控制长度**：列表最多列 10 条，更多的折叠成「略去更早 N 条」；单条错误信息截到 120 字左右。飞书请求体上限约 20KB，超了会被拒。
- 不要 @所有人。

### 4.2 两种形态怎么选

- **纯文本**：一两行就能说清的告警、Shell 脚本里发的通知。
- **卡片**：多段内容、要用颜色区分状态的（日报、健康状态、汇总）。

### 4.3 纯文本 payload

```json
{
  "msg_type": "text",
  "content": { "text": "[subtitleops] R2 备份失败\n原因：上传超时\n时间：2026-10-01 03:00:12 (Asia/Shanghai)" }
}
```

### 4.4 卡片 payload

```json
{
  "msg_type": "interactive",
  "card": {
    "config": { "wide_screen_mode": true },
    "header": {
      "template": "red",
      "title": { "tag": "plain_text", "content": "[subtitleops] Monitor 日报 · 2026-10-01" }
    },
    "elements": [
      { "tag": "div", "text": { "tag": "lark_md", "content": "**任务执行**\n• 抓取　✅12 ❌1\n• 转写　✅12" } },
      { "tag": "hr" },
      { "tag": "note", "elements": [ { "tag": "lark_md", "content": "数据截至推送时" } ] }
    ]
  }
}
```

- `header.template` 是标题栏颜色，按级别统一：

  | 级别 | 颜色 | 含义 |
  | --- | --- | --- |
  | `error` | `red` | 失败 / 严重 |
  | `warning` | `orange` | 需要关注 |
  | `success` | `green` | 成功 / 已恢复 |
  | `info` | `blue` | 普通提醒 / 待办 |

- `elements` 常用三种：`div`（正文，`lark_md` 支持 `**加粗**`、`[链接](url)`、`\n` 换行、`_斜体_`）、`hr`（分割线）、`note`（底部灰色小字，放数据口径说明）。

## 5. 发送规则（硬约束）

这几条都是踩过坑之后定的，不要省。

1. **成功 = 响应体里 `code == 0`**。HTTP 200 不算成功——飞书参数错、被限流时也返回 200，只是 `code` 非 0。老接口返回的是 `StatusCode`，两个都要认。响应不是 JSON 对象、或两个字段都没有，一律算失败。
2. **发送函数永不抛异常，只返回 `True` / `False`**。通知挂了不能拖垮主流程。
3. **必须设超时**（5 秒；Shell 里 10 秒）。
4. **没配 webhook 就静默跳过**，返回 `False`，不报错。
5. **确认送达后才能推进状态**。凡是「发过就不再发」的逻辑（冷却时间戳、已通知标记、水位线），只在发送返回 `True` 之后才写。否则一次网络抖动就把告警永久吞掉。
6. **需要保证送达的通知要能重试**（例如「有人提交了申请」）：把待通知状态落库，失败后退避重试，不要用一次性的后台任务发完就忘。普通告警不需要，下一轮自然会再评估。
7. **不要让飞书请求走代理**。项目里如果给抓取之类配了代理，只注入到对应的子进程 / 客户端，**绝不设全局 `http_proxy`**；确实设了的话，把 `.feishu.cn` 加进 `no_proxy`。
8. **多实例部署时只让一个实例发**定时类通知（日报等），否则会重复。
9. **支持 dry-run**：定时告警脚本提供 `--dry-run`，只打印要发的内容、不真发、不推进状态。

### 降噪（高频事件必做）

| 手段 | 做法 |
| --- | --- |
| **聚合** | 不要一条失败发一条。按固定周期（如 5 分钟）扫一次，把同类失败合并成一条：`过去 5min 内 3 类共 17 次`，每类附一条示例错误。 |
| **冷却** | 同一条规则触发后，N 分钟内（默认 60）不重复发。冷却状态存文件 / 数据库，进程重启不丢。 |
| **去抖** | 健康探测类不要一次失败就告警，连续失败 N 次（如 3 次）才发；恢复也要连续成功 N 次才发「已恢复」。 |
| **阈值** | 偶发的小量错误不告警，超过阈值才发。阈值做成环境变量可调。 |

## 6. 参考实现

三选一（或按需组合），整段复制，改 `PROJECT`。都不依赖第三方库。

### 6.1 Python

```python
"""飞书群通知（自定义机器人 webhook）。标准见 docs/feishu-notify-standard.md。"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Any, Callable, Dict, Optional

PROJECT = "改成项目名"
WEBHOOK_ENV = "FEISHU_NOTIFY_WEBHOOK"
TIMEOUT_SECONDS = 5
LEVEL_COLORS = {"error": "red", "warning": "orange", "success": "green", "info": "blue"}

# poster(url, payload) -> 飞书响应（已解析的 JSON）。测试时注入假的，不真发。
Poster = Callable[[str, Dict[str, Any]], Any]


def _http_post(url: str, payload: Dict[str, Any]) -> Any:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_feishu_message(
    payload: Dict[str, Any],
    *,
    webhook_url: Optional[str] = None,
    poster: Optional[Poster] = None,
) -> bool:
    """发一条飞书消息。仅当飞书确认收下（code == 0）才返回 True；永不抛异常。"""
    url = (webhook_url or os.environ.get(WEBHOOK_ENV) or "").strip()
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
```

调用：

```python
notify_text("R2 备份失败", "原因：上传超时\n时间：2026-10-01 03:00:12 (Asia/Shanghai)")

notify_card(
    "Monitor 日报 · 2026-10-01",
    "**任务执行**\n• 抓取　✅12 ❌1\n• 转写　✅12",
    level="error",
    note="数据截至推送时",
)
```

### 6.2 TypeScript（Node 18+）

```ts
// 飞书群通知（自定义机器人 webhook）。标准见 docs/feishu-notify-standard.md。

const PROJECT = "改成项目名";
const WEBHOOK_ENV = "FEISHU_NOTIFY_WEBHOOK";
const TIMEOUT_MS = 5000;
const LEVEL_COLORS = { error: "red", warning: "orange", success: "green", info: "blue" } as const;

export type NotifyLevel = keyof typeof LEVEL_COLORS;
export type FeishuPayload = Record<string, unknown>;
// poster(url, payload) -> 飞书响应（已解析的 JSON）。测试时注入假的，不真发。
export type Poster = (url: string, payload: FeishuPayload) => Promise<unknown>;
export interface SendOptions {
  webhookUrl?: string;
  poster?: Poster;
}

async function httpPost(url: string, payload: FeishuPayload): Promise<unknown> {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

/** 发一条飞书消息。仅当飞书确认收下（code === 0）才返回 true；永不抛异常。 */
export async function postFeishuMessage(payload: FeishuPayload, opts: SendOptions = {}): Promise<boolean> {
  const url = (opts.webhookUrl ?? process.env[WEBHOOK_ENV] ?? "").trim();
  if (!url) return false; // 没配 webhook：静默跳过
  let data: unknown;
  try {
    data = await (opts.poster ?? httpPost)(url, payload);
  } catch (err) {
    // 只打异常类型：异常信息里可能带完整 webhook URL，不能进日志
    console.error(`[feishu] 发送失败: ${err instanceof Error ? err.name : typeof err}`);
    return false;
  }
  if (typeof data !== "object" || data === null || Array.isArray(data)) {
    console.error("[feishu] 响应不是 JSON 对象，视为未送达");
    return false;
  }
  const body = data as Record<string, unknown>;
  const code = "code" in body ? body.code : body.StatusCode;
  if (code !== 0) {
    console.error(`[feishu] 飞书未收下: code=${String(code)} msg=${String(body.msg)}`);
    return false;
  }
  return true;
}

/** 纯文本通知。第一行自动加 [项目名] 前缀。 */
export function notifyText(title: string, body = "", opts: SendOptions = {}): Promise<boolean> {
  const text = `[${PROJECT}] ${title}` + (body ? `\n${body}` : "");
  return postFeishuMessage({ msg_type: "text", content: { text } }, opts);
}

/** 卡片通知。level 决定标题栏颜色。 */
export function notifyCard(
  title: string,
  markdown: string,
  opts: SendOptions & { level?: NotifyLevel; note?: string } = {},
): Promise<boolean> {
  const elements: unknown[] = [{ tag: "div", text: { tag: "lark_md", content: markdown } }];
  if (opts.note) {
    elements.push({ tag: "hr" });
    elements.push({ tag: "note", elements: [{ tag: "lark_md", content: opts.note }] });
  }
  const card = {
    config: { wide_screen_mode: true },
    header: {
      template: LEVEL_COLORS[opts.level ?? "info"],
      title: { tag: "plain_text", content: `[${PROJECT}] ${title}` },
    },
    elements,
  };
  return postFeishuMessage({ msg_type: "interactive", card }, opts);
}
```

### 6.3 Shell（备份 / 部署脚本用）

```bash
# 飞书文本通知。没配 FEISHU_NOTIFY_WEBHOOK 就跳过；失败不影响脚本继续。
# 用 python3 生成 JSON：消息里有引号、换行时，手拼 JSON 字符串会坏。
# 不校验飞书响应里的 code——Shell 里只做尽力而为的通知，要可靠送达请用 Python/TS 版。
notify_feishu() {
  [[ -n "${FEISHU_NOTIFY_WEBHOOK:-}" ]] || return 0
  local body
  body="$(python3 -c 'import json,sys; print(json.dumps({"msg_type":"text","content":{"text":sys.argv[1]}}, ensure_ascii=False))' "$1")" || return 0
  curl -fsS -m 10 -X POST "$FEISHU_NOTIFY_WEBHOOK" \
    -H 'Content-Type: application/json' -d "$body" >/dev/null 2>&1 || true
}

notify_feishu "[项目名] 备份失败
原因：上传超时
时间：$(date '+%Y-%m-%d %H:%M:%S %Z')"
```

失败告警建议统一挂在 `trap ... EXIT` 里按退出码判断，而不是 `trap ... ERR`——`set -u` 的未定义变量这类错误不会触发 ERR。

## 7. 测试与验收

- **单元测试不真发**：给发送函数注入 `poster`，断言 payload 结构和返回值。至少覆盖四种响应：`{"code": 0}` → True；`{"StatusCode": 0}` → True；`{"code": 19024}` → False；`poster` 抛异常 → False。再加一条：没配 webhook → False 且 `poster` 没被调用。
- **状态推进测试**：发送返回 False 时，冷却 / 已通知标记不能变。
- **上线前真发一条**：这一步会在真实群里出现消息，先跟用户确认再做。

## 8. 飞书侧限制与常见错误

- 频率限制：单个机器人 **100 次/分钟、5 次/秒**。超了返回 `code=11232`。批量事件靠聚合解决，不要靠重试硬顶。
- 请求体不超过约 **20KB**。
- 常见非 0 `code`：

  | code | 含义 | 处理 |
  | --- | --- | --- |
  | `19001` | webhook 地址无效 | 检查 `.env` 里的 URL 是否完整、机器人是否被移出群 |
  | `19021` | 签名校验失败 | 机器人被开了签名校验；本标准不带签名，找用户确认机器人设置 |
  | `19022` | IP 不在白名单 | 机器人被开了 IP 白名单 |
  | `19024` | 未包含自定义关键词 | 机器人被设了关键词，消息里必须含该词 |
  | `11232` | 触发限流 | 降低频率、做聚合 |
  | `9499` | 请求格式错误 | 检查 payload 结构（`msg_type` / `content` / `card`） |

- 官方文档：<https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot>
