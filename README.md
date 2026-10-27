# Claude Code + Codex + Kimi Usage Widget

一个 macOS 菜单栏小组件（基于 [SwiftBar](https://github.com/swiftbar/SwiftBar)），
在同一个 SwiftBar 入口中分区显示 **Claude Code**、**Codex** 与 **Kimi Code** 的
官方用量，并把「已用用量%」和「已过时间%」并排对比，帮你判断烧钱节奏。同时把
每个窗口终点时的用量记进日志。

Claude 可用时，菜单栏图标为六列（`C5h`、`C7d`、`X5h`、`X7d`、`K5h`、`K30d`），
每列两行：**时间% / 用量%**；某一侧不可用时只隐藏该侧的列；
点开后是三个独立的 iOS 风格分区：Claude Code 保留 5 小时与 7 天窗口；为避免
SwiftBar 对过高菜单图片重采样，Claude 的两个窗口分别使用一张 Retina 图片，并用
菜单分隔线隔开，避免 SwiftBar 2.1.1 把连续的第二张图片从低分辨率中间位图绘制；
Codex 显示 5 小时（视套餐）与 7 天窗口；Kimi 显示 5 小时窗口与订阅周期窗口
（约 30 天，即 Kimi 控制台的「总使用量」，重置时间跟随下次续费时间）。
两侧独立降级：任一侧抓取失败、登录失效或接口返回空用量时，仅该分区显示
“重新登录/无数据”，其余分区仍正常刷新和展示。
每张进度图会在 SwiftBar 输出里显式携带 Retina 点尺寸，并保持为单窗口高度，避免
SwiftBar 2.1+ 在 macOS 26 上把未声明尺寸的下拉图片压缩成 `16×16` 图标，或对 Claude
双窗口长图二次缩放而造成文字模糊。

## 工作原理

SwiftBar 每 5 分钟触发一次 [`plugins/claude-usage.5m.sh`](plugins/claude-usage.5m.sh)
（**SwiftBar 本身就是调度器**——间隔写在文件名的 `.5m.` 里，没有用 launchd/cron），它：

1. 跑 [`fetch_usage.py`](fetch_usage.py) 抓 Claude 官方用量，写入 `last_fetch.json`；
2. 跑 [`fetch_codex_usage.py`](fetch_codex_usage.py) 读取 Codex 官方限额快照，写入
   `codex_last_fetch.json`；
3. 跑 [`fetch_kimi_usage.py`](fetch_kimi_usage.py) 调 Kimi 官网计费接口抓 Kimi Code
   用量，写入 `kimi_last_fetch.json`；
4. 跑 [`render.py`](render.py)，把三份数据生成同一个 SwiftBar 菜栏入口与三个分区。

两份本次结果文件都用同目录临时文件写入，再原子替换到 `last_fetch.json` /
`codex_last_fetch.json` / `kimi_last_fetch.json`。这样自动刷新、手动“立即刷新”和
SwiftBar 自己的重跑即使短暂重叠，读方也不会看到半截 JSON 或空文件。

时间% 由 `render.py` 在每个 tick 本地重算，倒计时随之更新，因此显示始终是「活」的，
不必每 tick 都打接口。

Claude `/usage` 的 `utilization` 本身就是百分数（`1.0` 表示 **1%**，不是 100%）；
渲染与窗口日志都按百分数直接读取并限制在 `0%`～`100%`，避免 1% 边界被误放大。

### Codex 数据来源

本工具优先通过 Codex 官方 app-server 的只读方法 `account/rateLimits/read` 获取账号级限额；
只有官方接口不可用时，才降级读取 `~/.codex/sessions/**/*.jsonl` 的 `token_count` 事件，
该降级数据可能是模型级限额，不保证与官方账号页面一致。
它只保留 `rate_limits` 字段，识别 10080 分钟周窗口；不读取
`~/.codex/auth.json`，也不复制登录凭据。

Codex 面板通常每 5 分钟从 app-server 更新；`usedPercent` 按官方字段语义直接解释为百分比数值
（例如 `1` = `1%`，不是 `100%`）；降级到 JSONL 时，则会在 Codex 完成请求并
写出新事件后更新。JSONL 同时包含对话内容，脚本只保留命中的限额对象，绝不会缓存或
打印完整事件行。

Codex 的 `limit_id` 可能随账户或模型限额桶变化（例如 `codex_bengalfox`），因此降级
读取 JSONL 时按时间选择最新的限额事件，不固定使用某个旧的 `limit_id`。

### Kimi 数据来源

Kimi 分区走 Kimi 官网的 Connect-RPC 网关，与 kimi.com 设置页和 Kimi Code 控制台
完全同源（**全程只读**，不碰 `kimiCode` API Key）：

1. 从 Kimi 桌面端的 daimon 配置
   （`~/Library/Application Support/kimi-desktop/daimon-share/daimon/config.json`）
   读出 `credentials.kimiWeb.accessToken`（kimi.com 网页会话 JWT，桌面端常驻保活，
   有效期 15 分钟）。令牌每次现读现用，**不落明文、不打印、不缓存**。
2. `POST https://www.kimi.com/apiv2/kimi.gateway.billing.v1.BillingService/GetUsages`
   （`{"scope": ["FEATURE_CODING"]}`）拿到 5 小时滚动窗口与总额度；
   再调 `kimi.gateway.order.v1.SubscriptionService/GetSubscription` 拿到订阅周期的
   起止时间（`currentStartTime` / `nextBillingTime`）。
3. Kimi 控制台只展示两根条：**5 小时用量** 与 **总使用量**（订阅周期窗口，约 30 天，
   重置时间 = 下次续费时间），本组件一一对应为 `K5h` 与 `K30d` 两列。
   额度本身是百分数归一化的（`limit: "100"` = 100%），用量% = 100 − remaining。
   注意：老套餐的 7 天周限额在新控制台已不再展示，本组件也不映射它。

accessToken 过期后由 Kimi 桌面端自动刷新；若桌面端退出登录，Kimi 分区会显示
“重新登录”，不影响 Claude / Codex 分区。

### 凭据模型（重要）

本组件走的是 **Claude Desktop 的网页会话**，而不是 Claude Code CLI 的钥匙串 OAuth——
因为 Desktop 是个 claude.ai 的 Electron 网页壳，只要你在用 Desktop，它的登录 cookie
就一直保持新鲜。链路如下（**全程只读**，从不写 cookie 库或钥匙串）：

1. 从 Desktop 的 Chromium cookie 库
   （`~/Library/Application Support/Claude/Cookies`）读出 `.claude.ai` 的
   **`sessionKey`** cookie（加密存储）。这仅用于 Claude 分区。
2. 用钥匙串里的 **`Claude Safe Storage`** 密钥解密（Chromium `v10` 方案：
   PBKDF2-HMAC-SHA1(key, salt=`saltysalt`, 1003 轮, 16 字节) → AES-128-CBC，
   IV=16 个空格，去掉 `v10` 前缀与 PKCS7 padding，再剥掉新版 Chromium 在明文前
   加的 32 字节 `SHA256(domain)`）。得到 `sk-ant-sid02-…`。
3. 自动发现聊天组织：`GET /api/organizations`，挑 `capabilities` 含
   `chat`/`claude_max` 的那个（结果缓存进 `.org.json`，不写死 UUID）。
4. `GET https://claude.ai/api/organizations/{org}/usage`，带
   `Cookie: sessionKey=…` **和浏览器 User-Agent**（不带 UA 会返回空响应），
   拿到 `{ five_hour, seven_day, … }`——与 CLI OAuth 的
   `api.anthropic.com/api/oauth/usage` 结构一致。

sessionKey 每次抓取都现解现用，不落明文；**不做任何 token 刷新**。

### 限流与降级

- `fetch_usage.py` 用 `.last_attempt` 触碰文件把真实请求节流到 **每 5 分钟一次**，
  其余 tick 复用上次成功的数据（`usage_raw.json`）；`fetch_kimi_usage.py` 用
  `.kimi_last_attempt` 做同样的节流。
- Claude API 或 Cloudflare 偶发返回非 JSON 页面（如瞬时 `302 Found`）时，单次抓取会按
  `立即 → 3 秒后 → 10 秒后` 有界尝试 3 次。全部失败时继续展示 `usage_raw.json`
  中上一次成功数据，不把瞬时网络故障误报为“重新登录”。
- `usage_raw.json` 只保留可解析的上次好数据；本次 Claude 抓取异常会写入
  `last_fetch.json`；只有 Cookie 缺失、HTTP `401/403` 或用量数据结构明确不可用时，
  Claude 分区才显示“重新登录”，不会影响 Codex 分区。
- 刷新日志中的时间固定标记为 `Asia/Shanghai (UTC+8)`。

## 演进史（Before → After）

今天（2026-07-02）踩了个坑，把凭据方案整个换掉了。留档以便日后知道「之前怎么做、
现在为什么这么做」。

### 1. 凭据：CLI 钥匙串 OAuth → Desktop 网页 cookie

**Before（旧方案，已废弃）**

- 读 Claude Code **CLI** 的钥匙串 OAuth 凭据：service = `Claude Code-credentials`，
  account = 用户名；access token 临近过期时调 `console.anthropic.com/v1/oauth/token`
  刷新，结果写进私有缓存 `.token.json`（600 权限），只读不写 CLI 的钥匙串条目。
- **为什么废弃**：使用者其实只用 **Desktop**、几乎不碰 CLI。CLI 的 access token
  （约 8 小时寿命）一旦没人用就过期；过期后组件想自己刷新，却持续撞到刷新接口的
  限流（`.token.json` 从未成功生成过一次），于是每 5 分钟拿着死 token 去撞限流、
  越撞越起不来，反而拖慢了 Claude Code 本体的刷新。结果 widget 长时间空白，
  只能靠人手动去终端跑一次 `claude` 才「偶然」恢复。
- 另一个隐患：CLI 与 Desktop 是**两套完全独立的凭据体系**（这也是为什么 CLI
  读不到 Desktop 的对话）。钥匙串里堆了 ~85 个带后缀的
  `Claude Code-credentials-<hex>`，多半就是 refresh token 轮换 / 反复重登 churn 出来的。

**After（现方案）**

- 改读 **Desktop 的 claude.ai `sessionKey` cookie**（见上「凭据模型」）。
- 好处：**不依赖 CLI、不刷新、不撞限流**；只要在用 Desktop，凭据天然新鲜。
- 代价：若 Desktop 退出登录 / 换账号，cookie 没了就抓不到——重新登录 Desktop 即可。

### 2. 窗口日志：滚动检测 → 按墙钟封窗

**Before**：只有当观测到 `resets_at` 滚动到**新窗口**时，才把上一个窗口封存写日志。

**为什么改**：一个刚开、还没用量的 5 小时窗口，接口返回的 `resets_at` 是 `null`。
旧逻辑遇到 `null` 直接跳过，导致刚结束的窗口迟迟封不了、它的最终用量一直卡在
`window_state.json` 里没落盘（真实踩坑：16:50→21:50 窗口的 38% 就这么丢在状态里）。

**After**：只要**当前时间已过某窗口的 `resets_at`** 且尚未记录，立刻用「结束前最后
观测到的用量」封存该窗口——不再依赖下一个窗口是否空闲。即便中途 SwiftBar 没运行 /
token 有空档，恢复后也会按墙钟补记，记录不丢。仍然**每个窗口只写一行**（用容差判断
换窗，抖动一两秒不误记）。

## 用量窗口日志

[`window_log.py`](window_log.py) 记录每个 **7 天窗口**和每个 **5 小时窗口**在
**终点（重置时刻）**的用量百分比。它在 `fetch_usage.py` 每次**抓取成功后**自动调用，
日志出错绝不会影响用量抓取本身。

产出两份日志（同一批事件）：

| 文件 | 用途 |
| --- | --- |
| `window_log.jsonl` | 原始机器记录，一行一个 JSON，**唯一真相来源** |
| `window_log.md` | 人看的 Markdown 表格，每次窗口关闭时从 JSONL 重新渲染 |

`window_log.md` 每个窗口一张表，列含：**日期 / 窗口起点 / 窗口终点（首尾）/ 终点用量%**，
时间固定为北京时间（Asia/Shanghai, UTC+8）。

手动更新 / 重新生成 MD：

```bash
python3 window_log.py
```

## 安装

1. 安装 SwiftBar：`brew install swiftbar`
2. 装依赖：`python3`、`curl`、`openssl`（macOS 自带），以及 Python 的 `Pillow`
   （`pip3 install Pillow`）
3. 把本目录放在 `~/claude-usage-widget`（脚本按此路径寻址）
4. 在 SwiftBar 里把插件目录指向 `plugins/`，或把
   [`claude-usage.5m.sh`](plugins/claude-usage.5m.sh) 软链进你的 SwiftBar 插件目录
5. **确保 Claude Desktop 已登录**（Claude 分区的数据来源），并至少在 Codex 中完成过
   一次请求（Codex 分区才会产生本地限额快照）
6. **确保 Kimi 桌面端（Kimi Work）已登录**（Kimi 分区的 accessToken 来源；
   桌面端常驻运行时令牌自动保活）

## 文件一览

| 文件 | 作用 |
| --- | --- |
| `plugins/claude-usage.5m.sh` | SwiftBar 入口，每 5 分钟 tick |
| `fetch_usage.py` | 解密 Desktop cookie，调 claude.ai 用量接口；限流节流 |
| `fetch_codex_usage.py` | 只读 Codex 本地会话中的官方限额快照，不读取凭据 |
| `fetch_kimi_usage.py` | 复用 Kimi 桌面端网页会话，调官网计费接口；限流节流 |
| `render.py` | 读三侧缓存，生成共用菜单栏 + 三个独立面板 |
| `imggen.py` | 把菜单栏图标与面板画成 iOS 风格 PNG（2x 视网膜）|
| `window_log.py` | 记录每个窗口终点用量，输出 JSONL + MD 日志 |
| `diag.py` | 只读排错工具（不打印任何密钥）|
| `usage_raw.json` | 上次成功的用量数据（缓存 / 降级用）|
| `last_fetch.json` | 本次 tick 的抓取结果（成功数据或错误）|
| `codex_last_fetch.json` | 本次 tick 读取到的 Codex 限额快照或错误 |
| `kimi_last_fetch.json` | 本次 tick 读取到的 Kimi 用量快照或错误 |
| `.org.json` | 缓存的聊天组织 UUID（非机密）|
| `.last_attempt` / `.kimi_last_attempt` | 节流触碰文件，把真实请求限到每 5 分钟一次 |
| `window_log.jsonl` / `window_log.md` | 窗口终点用量日志（原始 / 人看）|
| `window_state.json` | 各窗口当前进行中的状态（供封窗检测）|

## 额度用满飞书推送
任一订阅（Claude / Codex / Kimi）的任一窗口（5 小时、7 天、订阅周期 30 天）用量到 100%，会往飞书群发一张卡片；同一个窗口在同一个重置周期内只发一次；窗口重置、用量回落后再发一张绿色的“额度已恢复”卡片。
配置：复制 `.env.example` 为 `.env`，填入 `FEISHU_NOTIFY_WEBHOOK`（留空 = 不推送）。预览不真发：`python3 render.py --alerts-dry-run`（只打印将要发的内容，不真发、不记状态）。标准见 `docs/feishu-notify-standard.md`。

## 安全须知

- **只读** Desktop 的 cookie 库和 `Claude Safe Storage` 钥匙串项，从不写回；
  也不碰 Claude Code CLI 的钥匙串条目。
- sessionKey 每次现解现用，**不落明文**、不打印。
- `.org.json` 只存组织 UUID（标识符，非机密）。
- Codex 分区不读取 `~/.codex/auth.json`；仅从会话 JSONL 中抽取限额字段，不保留对话。
- Kimi 分区只读 daimon 配置取 accessToken，每次现读现用，**不落明文**、不打印、
  不缓存；不使用 `kimiCode` API Key，也不对 Kimi 桌面端做任何写入。

## 任务看门狗（Multica 断线任务自动复活）

`usage_watchdog.py` 由 launchd 每 15 分钟跑一次（`com.llm-usage.watchdog`）：找出 `in_progress`
且负责人是 agent、但超过 25 分钟没有活动的 Multica 任务，确认对应供应商（按 agent 绑定的
runtime 映射到 Claude / Codex / Kimi，用量读本目录的三份快照）5 小时窗口已恢复后，
重新 assign 触发新 run 复活它。防抖：同一任务 6 小时内最多复活 2 次、间隔至少 1 小时；
快照超过 15 分钟未刷新按「额度未知」处理不误动。

模式开关在 SwiftBar 菜单底部「任务看门狗」：**自动复活** / **仅记录不复活**（写在
`watchdog_mode.txt`，`apply` / `dry`；命令行 `--apply` / `--dry` 可临时覆盖）。
日志在 `logs/watchdog.log`，每次运行自动清理，只保留最近 2 天。
卸载：`launchctl bootout gui/$(id -u)/com.llm-usage.watchdog` 并删除
`~/Library/LaunchAgents/com.llm-usage.watchdog.plist`。
