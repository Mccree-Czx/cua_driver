# 云机执行端（.66）的启动与调度

这台机器 `10.99.40.66` 是**猎聘的云机专用执行端**——代码里点名的角色，见
`agent/run_round.py` 的 `ensure_device()` 第 2 档（`HR_CLOUD_PHONE=1`）。

它驱动的不是真机，而是本机**多多云客户端**（`ddyun.exe`）里跑的一台云手机。
云机暴露成本机的一个 adb TCP 端点，锚点表是
[`agent/login_map.cloud.json`](../../agent/login_map.cloud.json)。

## 怎么跑

```cmd
scripts\cloud\round.cmd backfill_round
scripts\cloud\round.cmd main_round
scripts\cloud\round.cmd daily_round
```

日志：`<仓库>\.run\logs\cloud-round.log`（包装脚本的），
以及 `agent\.round_<轮次名>.log`（Pi 自己的输出）。

## 为什么是这三个环境变量（**这是本目录存在的全部理由**）

`round.cmd` 里设的每一条都不是可有可无的，删掉任何一条都会有具体症状：

### `ANDROID_HOME=C:\` —— 没有它，Pi 一个手机工具都没有

**最值钱的一条。** `mobile-mcp` 底层的 `mobile-use` 解析 adb 时
（`mobile-use/dist/index.js` 的 `getAdbPath()`）：

- 只在**一张写死的候选路径表**里挨个 `existsSync`——Windows 上是
  `%LOCALAPPDATA%\Android\Sdk\...`、`C:\Android\sdk\...` 等 6 条；
- **从不查 PATH**；6 条都不中就 `throw "ADB not found"`；
- 能改的只有一个开关：`ANDROID_HOME`，它会拼上 `platform-tools\adb.exe`。

而本机的 adb 只在 `C:\platform-tools\adb.exe`（那 6 条候选一条都不存在），
所以 `ANDROID_HOME=C:\` 正好拼出这个路径。

**为什么症状会很隐蔽**：`mobile-mcp/dist/mcp_server.js` 在**模块顶层**就
`new ADBClient()`，所以这个异常发生在 MCP server 启动的那一刻——
Pi 那边表现为「mobile 这个 server 起不来」，而不是「点错了」。

（`C:\platform-tools\adb.exe` 这个位置本身是云机执行端的约定，
`shared/android_ui.py` 与 `tools_mcp_server._resolve_adb()` 里都补了这条候选路径。）

### `HR_CLOUD_PHONE=1` —— 没有它，轮次会去驱动真机

云机的 adb 端口**不是固定的**（实测一次会话内从 63833 变到 51639）。
设了这个变量，`ensure_device()` 才会用 `discover_cloud_phone()` 现找端口并写进
`ANDROID_SERIAL`；找不到就**拒跑这一轮**，不猜。

不设的后果不是「少干一轮」：`flows.map_path()` 会因此走进「serial 不含冒号 →
真机表」那条分支，**拿 1080x2244 的坐标去点 720x1280 的云机屏**。

★ **不要对云机 `adb disconnect`**——实测那一下会让客户端把 adb 端点整个关掉，
回来时已经换到新端口了。端口变了这样找（管理员 PowerShell）：

```powershell
Get-NetTCPConnection -State Listen |
  Where-Object { $_.OwningProcess -eq (Get-Process ddyun).Id }
```

### `HR_PI_AGENT_DIR` —— 没有它，会和本机那个通用 Pi 串在一起

本机 `~/.pi/agent` 是**用户的通用 pi 安装**（2026-08 就有 session 了）。
Pi 从「配置目录」读 `mcp.json`，两边共用一份的话，用户自己开 pi 时会连带把
本系统的 `hr-tools`（**直连生产 MySQL/MinIO**）和 `mobile-mcp`（**抢 adb**）
一起拉起来。所以本系统用 `~/.pi/hr-agent`，`mcp.json` 也放在那里。

`mcp.json` 的内容（Pi 的格式与其它 MCP 客户端一致，见 pi 包内 `docs/mcp.md`）——
可直接填的模板在 [`mcp.json.example`](mcp.json.example)，把 `<REPO>` / `<NODE>` /
`<MOBILE_MCP_ENTRY>` / `<VENV_PYTHON>` / `<ANDROID_HOME>` 换成实际路径即可
（JSON 不支持注释，所以说明只在本文件里，模板本身是干净的）：

```json
{
  "mcpServers": {
    "mobile": {
      "command": "C:\\Program Files\\nodejs\\node.exe",
      "args": ["C:\\Users\\Administrator\\AppData\\Roaming\\npm\\node_modules\\mobile-mcp\\dist\\index.js"],
      "env": { "ANDROID_HOME": "C:\\" }
    },
    "hr-tools": {
      "command": "C:\\cua_driver\\.venv\\Scripts\\python.exe",
      "args": ["C:\\cua_driver\\services\\pipeline\\tools_mcp_server.py"],
      "cwd": "C:\\cua_driver"
    }
  }
}
```

- 用 `node` + JS 入口，**不要用 npm 的 `.cmd` shim**：`CreateProcess` 直跑不了
  `.cmd`（`agent/run_round.py` 的 `_resolve_pi_cli()` 踩过同一个坑）。
- `ANDROID_HOME` 只放在 mobile 的 `env` 里，不动系统环境变量。
- 验证这两条通道是否真的通，用一次完整握手（不要只看进程起没起）：

  ```
  initialize → notifications/initialized → tools/list
  ```

  期望：mobile 9 个工具、hr-tools 32 个工具。**必须等上一条的响应再发下一条**——
  三条一起灌进去的话 `tools/list` 会抢在初始化完成前到达而被丢掉，看起来像
  「0 个工具」。

### 另外两条

- `PATH` 里补 nodejs 与 Python312\Scripts（计划任务不继承交互式 PATH），
  uv 是 `run_round` 读库时要用的。
- `NO_PROXY=127.0.0.1,localhost,::1`：httpx 的 `trust_env` 会把内网调用交给代理
  （症状：同一连接首请求 200、其后全 404）。

## ★ 写 `.cmd` 的两条硬规则

1. **ASCII only**——cmd 默认按 GBK 读脚本，中文注释在有些控制台上会直接崩。
   中文说明就写在本文档里。
2. **必须 CRLF 换行**。LF-only 的 `.cmd` 会被 cmd 解析错乱，
   症状是乱七八糟的 `'xxx' is not recognized as an internal or external command`
   ——报错的字符串看起来跟真正的元凶毫无关系（实测把一个 `REM` 行切成了
   `ud-phone` 去执行）。本仓库其它 `.cmd` 都是 CRLF。
3. **★ 绝不要在 `.cmd` 正在运行时编辑它。** cmd 是按**字节偏移**边读边执行的，
   文件一变，它就会从错位的地方接着读——实测（2026-10-16）在轮次跑到一半时改了
   `round.cmd` 的 PATH 行，结果是 **python 被启动了第二次**（日志里出现两条
   `start`、只有一条 `exit`，白跑一小时、还多花一份 token），末尾还执行了一截
   被切碎的命令。改脚本前后都要确认没有轮次在跑（`agent\.round.lock` 不存在）。

## 实测：云机比真机慢多少

**结论：看工作形态，不是一句话能概括的。**

### 普通候选处理：**基本持平**

2026-10-17 一轮完整的 `main_round`（处理 5 个候选、打 2 声招呼）：

| 指标 | 值 |
|---|---|
| 墙钟 | **307 秒（5.1 分钟）**，自然结束 `exit 0` |
| 手机操作 | 69 次 / 156 秒（占墙钟 51%） |
| **单次操作中位** | **2.4 秒**（p90 4.4s、最大 6.2s、**零次超 30 秒**） |
| tokens / 成本 | 2,746,886 / $0.0555 |

真机单次 `dump` 是 2–3 秒（`agent/flows.py:531`）——**云机在候选卡片/详情页这类小交互上
与真机基本持平**。所以 `main_round` 在云机上完全可行，5 分钟一轮，和「每 300 秒拉起一次」
的调度节奏是匹配的。

### 大列表滚动扫描：**约 2.3 倍慢**

| 口径 | 真机 | 云机 | 倍数 |
|---|---|---|---|
| 扫一遍职位列表 | ~60 秒（`docs/runbook.md:266`） | 实测 **137 秒** | 2.3× |

2026-10-16 那两轮 `backfill_round` 就是被这个拖垮的：它们反复扫 89 行会话列表，
单次调用中位 5.9–9.2 秒、p90 59.6 秒、最大 153 秒，17% 的调用超过 30 秒；
两轮各跑了 128 / 165 次手机调用、136 / 166 个 Pi 回合，都撞满 3600 秒超时。

★ **七成时间花在手机上，不在 LLM**：那两轮里约 2700 秒脚本时间 / 3600 秒墙钟。
「换个更快的模型」救不了这种轮次——要快只能减少手机操作次数（见下面的单轮预算）。

### ★ UI 文案漂移：推荐页的按钮叫「继续沟通」

云机的猎聘 App 是 `version_code 13060`（真机 13120）。**推荐页候选人详情页的主按钮
显示「继续沟通」，而 `system-prompt.md` / `rounds.md` 写的是「立即沟通」。**

Pi 实跑时的判断是对的：它 tap 之后进聊天页核对——
**没有历史会话的新候选人会真的发出招呼；已有会话的人只是打开旧对话、不会重复发消息**，
于是跳过且不记 `out` 流水（韦女士就是这么被正确跳过的）。

但这依赖 LLM 现场判断，**不是确定性保证**。改提示词或换 App 版本时要重新核这一条。

### 由此得出的单轮预算

`rounds.md` §3 的「单轮上限 20 个」是按真机写的，2.5 倍慢速下做不完，一轮会撞满
3600 秒超时——于是永远输出不了 `@@RESULT@@`、`outcome` 永远是 `timeout`，
空轮退避的判定也跟着失真。

所以本目录的 `round.cmd` 设 `HR_ROUND_MAX_ITEMS=5`，让每轮自然结束。
**不要靠调大 `DEFAULT_ROUND_TIMEOUT` 解决**：那个值同时是 `LOCK_STALE_SECONDS`
（3900s）的依据，调大意味着一个卡住的轮次能霸占手机更久。

### ★ 不要用 kill 来「让轮次停在第 N 声招呼」

`agent/login_watch.py:193` 记着同一条代价：硬中断可能打断一次**已经点出去、
但还没写 `interaction_log`** 的触达——「一人一消息」在那条路径上无法保证，
下一轮可能给同一个人发第二次招呼。

要限制一轮做多少，用 `HR_ROUND_MAX_GREETS`（`agent/run_round.py` 的
`_append_round_caps()` 会把它拼进本轮提示词），让 Pi 自己数着做、
按顺序落账后收尾。

## ★ 还没有的东西（用之前必须知道）

- **云机上登录 / 换号不可用。** `login_map.cloud.json` 的「登录」「退出」两节是
  `★未探`——要探必须先登出，而登出后需人工收短信验证码重登。
  在那之前 `flows.py` 会因为缺 `phone_login` 等键**直接 KeyError**（这是刻意的：
  宁可报错，也不要拿真机坐标去点云机）。
- 所以 **`round.cmd` 不接受 `login_round`，也不要给 `login_watch.py` 建计划任务**。
  每 20 秒跑一个必然失败的东西只是噪音。
- 职位扫描那节的滚动坐标标着 `★未复测`（当时账号只有 2 个职位、一屏放得下，
  没真滚过）。

## 与其它两套的关系

| | 猎聘（真机） | 猎聘（云机，本机） | BOSS |
|---|---|---|---|
| 在哪 | Mac `10.99.31.81` | **本机 `10.99.40.66`** | Windows + Mac 真机 |
| 驱动什么 | 真机 `10.99.22.219` | 多多云里的云手机 | 真机 `10.99.22.219` |
| 现状 | **launchd 三个任务停着**（真机让给 BOSS） | 本文档 | 在跑 |

云机的意义就在这里：猎聘挪到云机之后，真机可以完全让给 BOSS，
两套不用再抢同一部手机。两侧的库（`hr_workbuddy` / `boss_workbuddy`）与
Redis db（0 / 1）本来就是分开的。
