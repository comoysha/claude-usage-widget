# Claude Usage Widget

一个 macOS 菜单栏小组件（基于 [SwiftBar](https://github.com/swiftbar/SwiftBar)），
显示 Claude 的**官方用量**——和你在 Claude 里看到的 `/usage` 数字一致——并把
「已用用量%」和「已过时间%」并排对比，帮你判断烧钱节奏。同时把每个窗口终点时的
用量记进日志。

菜单栏图标为两行：**时间% / 用量%**；点开是一个 iOS 风格的面板，含两条圆角进度条
（5 小时窗口 + 7 天窗口）、节奏判断和重置倒计时。

## 工作原理

SwiftBar 每 5 分钟触发一次 [`plugins/claude-usage.5m.sh`](plugins/claude-usage.5m.sh)
（**SwiftBar 本身就是调度器**——间隔写在文件名的 `.5m.` 里，没有用 launchd/cron），它：

1. 跑 [`fetch_usage.py`](fetch_usage.py) 抓官方用量，输出写进 `last_fetch.json`；
2. 跑 [`render.py`](render.py)，读 `last_fetch.json` 生成 SwiftBar 菜单栏标记。

时间% 由 `render.py` 在每个 tick 本地重算，倒计时随之更新，因此显示始终是「活」的，
不必每 tick 都打接口。

### 凭据模型（重要）

本组件走的是 **Claude Desktop 的网页会话**，而不是 Claude Code CLI 的钥匙串 OAuth——
因为 Desktop 是个 claude.ai 的 Electron 网页壳，只要你在用 Desktop，它的登录 cookie
就一直保持新鲜。链路如下（**全程只读**，从不写 cookie 库或钥匙串）：

1. 从 Desktop 的 Chromium cookie 库
   （`~/Library/Application Support/Claude/Cookies`）读出 `.claude.ai` 的
   **`sessionKey`** cookie（加密存储）。
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
  其余 tick 复用上次成功的数据（`usage_raw.json`）。
- 任何瞬时错误（离线、cookie 暂时读不到等）都保留上次好数据继续显示，菜单栏不会挂。

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
时间为本地时区。

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
5. **确保 Claude Desktop 已登录**（cookie 库里有 `sessionKey`）——这是数据来源

## 文件一览

| 文件 | 作用 |
| --- | --- |
| `plugins/claude-usage.5m.sh` | SwiftBar 入口，每 5 分钟 tick |
| `fetch_usage.py` | 解密 Desktop cookie，调 claude.ai 用量接口；限流节流 |
| `render.py` | 读缓存，生成菜单栏 + 面板的 SwiftBar 标记 |
| `imggen.py` | 把菜单栏图标与面板画成 iOS 风格 PNG（2x 视网膜）|
| `window_log.py` | 记录每个窗口终点用量，输出 JSONL + MD 日志 |
| `diag.py` | 只读排错工具（不打印任何密钥）|
| `usage_raw.json` | 上次成功的用量数据（缓存 / 降级用）|
| `last_fetch.json` | 本次 tick 的抓取结果（成功数据或错误）|
| `.org.json` | 缓存的聊天组织 UUID（非机密）|
| `.last_attempt` | 节流触碰文件，把真实请求限到每 5 分钟一次 |
| `window_log.jsonl` / `window_log.md` | 窗口终点用量日志（原始 / 人看）|
| `window_state.json` | 各窗口当前进行中的状态（供封窗检测）|

## 安全须知

- **只读** Desktop 的 cookie 库和 `Claude Safe Storage` 钥匙串项，从不写回；
  也不碰 Claude Code CLI 的钥匙串条目。
- sessionKey 每次现解现用，**不落明文**、不打印。
- `.org.json` 只存组织 UUID（标识符，非机密）。
