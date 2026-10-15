# 架构证据索引（system-model.evidence.md）

- 范围：`cua_driver` 仓库（git 根）当前态 `main @ 4a7f939`（2026-10-13 提交），2026-10-14 建模。
- 方法：代码 / 配置 / Git 历史 / 运行时（Windows 任务计划、`~/.pi` 配置）交叉取证。
- 置信度：high = 代码/配置/运行时直接证据；medium = 多处部分信号一致；low = 仅命名/目录推断；unknown = 待验证。
- 图中节点 ID 与 `system-model.dsl`、`inbound-business-flow.dot` 对应。

## 1. 节点证据

| 节点 | 声明 | sourceRefs | 置信度 |
|---|---|---|---|
| Pi 内核（piCli） | 外部 AI 代理运行时，`@earendil-works/pi-coding-agent`，DeepSeek v4 flash | 工作区 `.claude/settings.json`（pi.cmd 路径与 --version 探测）；`agent/run_round.py:7` | high |
| mobile-mcp | 外部 MCP 服务，`npx mobile-mcp`，提供 9 个手机工具 | `C:\Users\Administrator\.pi\agent\mcp.json:3-8`；`agent/system-prompt.md:7` | high |
| hr-tools MCP | 自定义 stdio MCP，26 个 `@mcp.tool()`；复用 pipeline 模块；ADB 硬编码路径；Redis 桶与熔断键 | `services/pipeline/tools_mcp_server.py:1-44,46-49,59-76`；工具计数（Select-String=26） | high |
| 轮次运行器 | `python run_round.py main_round/daily_round`；窗口门 / 文件锁+PID 存活 / 空轮退避 / token 汇总 | `agent/run_round.py:1-9,25-33,36-113` | high |
| Windows 任务计划 | `hr-pi-main`（PT2M，2026-10-10 起）、`hr-pi-daily`（19:50），Execute=Python312 + run_round.py | 运行时 `Get-ScheduledTask`（本机实测） | high |
| pipeline | FastAPI :8000；启动即 `alembic upgrade head`；`/internal/*` 回调已随旧编排层删除（B 方案） | `services/pipeline/app/main.py:1-38`；`api.py:17-55`；`hr_api.py:28` | high |
| screening | FastAPI :8001；硬规则短路 →（`llm_scoring=False` 直返回）→ LLM 评星（`min_stars` 阈值）→ degraded | `services/screening/app/main.py:57-114`；`hr_workbuddy/models.py:67-94` | high |
| hr-console | Next.js + antd「OutIn HR Portal」；页面：首页漏斗 / candidates / scoring / logs / manual + alert-bar / app-shell | `apps/hr-console/app/`（目录）；`app/status.ts:3-15`；commit `aeacfe5` 改动清单 | high |
| MySQL | 6 张业务表；状态字段 VARCHAR(32) 存 11 态枚举；ENUM 用于 source/direction/msg_type | `services/pipeline/app/models.py:43-218`；迁移 `alembic/versions/0001~0008` | high |
| MinIO | 快照 / 简历 HTML / 附件 PDF；预签名 ≤15min | `models.py:82-87`；`hr_api.py:30,55-64`；`infra/docker-compose.yml:35-69` | high |
| Redis 7 | 令牌桶（`hr_workbuddy/rate_limit.py`）、风控熔断键 `cua:risk:paused`、派发登记（遗留读取） | `tools_mcp_server.py:46,59-66`；`packages/contracts/hr_workbuddy/rate_limit.py` | high |
| HR 运营 | 唯一人类用户；门户只读 + review/ack/PATCH 动作 | `hr_api.py:505,414,544`（动作端点仅此三类 POST/PATCH） | high |
| 猎聘 App | 包名 `com.lietou.mishu`；读取与触达均在手机端完成 | `agent/system-prompt.md:9` | high |

## 2. 关键机制证据

| 机制 | 说明 | sourceRefs | 置信度 |
|---|---|---|---|
| 一人一消息 | jc 级 `ensure_one_message` + 候选人级 `candidate_has_out_message` 双查；`interaction_log` 对第二次 out 拒绝 | `agent/system-prompt.md:20`；`rounds.md:20,32`；`tools_mcp_server.py`（引用 messaging 模块） | high |
| gated_tap 双闸门 | 触达点击必须走 `gated_tap(touch=True)`：服务端先过风控熔断 + 每日限频，不过则物理点不出去；签名含 `daily_cap/counts_daily` | `system-prompt.md:19`；`tools_mcp_server.py`（`def gated_tap(x, y, touch=False, daily_cap=DAILY_TOUCH_CAP, counts_daily=True)`） | high |
| 附件证据守卫 | `receive_resume` / `interaction_log(attachment)` 需候选人 `attachment_keys` 证据（仅 `capture_attachment_pages` 写入）；在线简历截图不算附件 | `models.py:84-87`；`tools_mcp_server.py:79-86`；`rounds.md:31-32` | high |
| 落账顺序铁律 | 触达 → 验证上屏 → `interaction_log` → `state_transition` → `tasklog_add`，顺序颠倒被工具拒绝 | `system-prompt.md:24,26` | high |
| 风控即停 + 全局熔断 | 页面出现风控/验证码 → 停止 + `risk_set()` 写全局键，队列任务零操作跳过，人工验证后清除 | `system-prompt.md:21`；`tools_mcp_server.py:46` | high |
| 空轮退避 | 连续 2 个空轮后，30 分钟内跳过 main_round（`.inbound_state.json`） | `run_round.py:30-33,89-94` | high |
| @@RESULT@@ 机器可读结果 | 轮末输出 `inbound=N outbound=M` 供调度判空轮 | `rounds.md:38-41`；`run_round.py:97-101` | high |
| 成本落账 | Pi session jsonl 汇总 tokens/cost → `TaskLog` 表（tasklog_add） | `run_round.py:116-150`；`models.py:190-217` | high |
| 多职位均分打招呼 | 每职位日配额 floor(100/N)；职位 tab 选择、沟通职位模糊匹配 | `rounds.md:6-25`；`system-prompt.md:10-13` | high |

## 3. 演进证据（两代架构）

| 事件 | 证据 | 置信度 |
|---|---|---|
| 一代（M1/M2，spec v1.6）：scheduler + arq + cua-agent（桌面 Chrome/CUA SDK）+ 回调编排 | `AGENTS.md:87-116`；`docs/runbook.md:5-39`；`services/pipeline/app/task_queue.py:1-13` | high |
| cdp-typing 分支：桌面键鼠/文本输入改走 CDP（Windows 实盘校准） | worktree `worktree-cdp-typing @ e29b2b4`（2026-10-09），已全部并入 main（merge-base = e29b2b4） | high |
| 二代（B 方案，2026-10-09/10）：Pi 内核 + 手机 MCP；删除 cua-agent / scheduler / 旧编排 / 旧 spec / M1 E2E 脚本；console 迁 antd + 品牌 OutIn | commit `aeacfe5`（2026-10-10）stat：`services/cua-agent/*` 全删、`liepin-hr-assistant-spec-v1.6.md` -240 行、`scripts/run_m1_e2e.py` -1053 行、contracts 清理（`protocols.py` 删除） | high |
| main 删除内部回调端点 | `services/pipeline/app/main.py:5-6`「B 方案（2026-10-09）：内部回调端点（/internal/*）已随旧编排层删除——执行由 Pi 内核经 hr-tools 直写」 | high |

## 4. 遗留与兼容（当前仍存在但不属于主链路）

| 项 | 证据 | 置信度 |
|---|---|---|
| `task_queue.py`（arq 投递）与 `deps.get_queue` | `task_queue.py:1-13,38-70`；`deps.py:14` | high |
| `hr_api` manual-queue 读派发登记（`queue.get_dispatched`） | `hr_api.py:6-7,357-407` | high |
| `hr_api` 文档字符串残留「rerun 手动重跑」，但代码中已无对应 POST 端点（仅 ack/review/PATCH 三个动作端点） | `hr_api.py:1-8` vs `@router.post|patch` 实测 3 处（414/505/544） | high（死代码/文档漂移） |
| contracts 仍导出 `AtomicTask`（arq 时代契约） | `packages/contracts/hr_workbuddy/__init__.py:12-19` | high |
| `infra/worlds/*.json`（M1 mock 剧本）、`infra/tests`（底座冒烟） | 目录结构；`docker-compose.yml:1-88` | medium |
| `mobile/`（工作区级）：adb + uiautomator2 可行性探测 spike（probe/dump/tap）；`ADBKeyboard.apk` | `mobile/probe.py:1-8`；`.scratch/` 截图；工作区根 `ADBKeyboard.apk` | high |
| `agent/` 运行时文件：`.round_*.log`、`.inbound_state.json`、`.screening.log`、`_awaiting.json`、`_clip/` 截图 | git status 未跟踪清单；目录实测 | high |

## 5. 未知与验证任务

| # | 未知项 | 建议验证方式 |
|---|---|---|
| 1 | mobile-mcp 的版本、实现与稳定性（外部 npm 包，仓库无源码） | 查看 `npx mobile-mcp` 包信息与版本锁定方式 |
| 2 | Pi 的模型/参数配置细节（`~/.pi/agent/settings.json`、`models-store.json` 未取证） | 直接查阅 `~/.pi/agent/` 配置（注意 `auth.json` 含凭据，勿外传） |
| 3 | 风控触发频率与人工恢复的实际执行记录 | 检索 `agent/.round_*.log` 与 Pi sessions（`~/.pi/agent/sessions`） |
| 4 | backfill_round 的触发方式（计划任务中无对应项，疑为手动执行） | 查看 `hr-pi-*` 任务外的执行记录 / 用户确认 |
| 5 | 数据底座的实际运行形态：compose MySQL 默认不启用（本机 3306 被宿主 MySQL 占用），实际连的是哪个实例 | 查看仓库根 `.env` 的 `DATABASE_URL`（勿外传） |
| 6 | hr-console 是否还有其它数据来源（首页 stats、day 报表等端点未逐行核对） | 通读 `hr_api.py` 剩余只读端点 |
| 7 | 文档漂移：`AGENTS.md` / `README.md` / `docs/runbook.md` 仍按一代架构书写（描述不存在的 cua-agent/scheduler 服务） | 维护建议：以 `agent/system-prompt.md` + `rounds.md` + 代码为准，或安排文档修订 |
