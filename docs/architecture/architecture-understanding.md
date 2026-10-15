# hr_workbuddy 架构理解报告（当前态）

> 生成：2026-10-14 · 范围：`cua_driver` 仓库（git 根）`main @ 4a7f939` · 建模技能：system-modeler + c4model/graphviz 格式
> 配套产物：[system-model.dsl](./system-model.dsl)（C4 上下文/容器视图）、[inbound-business-flow.dot](./inbound-business-flow.dot)（业务闭环）、[system-model.evidence.md](./system-model.evidence.md)（证据索引）

## 一、一句话概括

这是**猎聘（Liepin）HR 招聘自动化系统**：一个 AI 代理（Pi）操作一台**真实安卓手机**上的猎聘 App，替 HR 完成「筛选候选人 → 打招呼 / 索要简历 → 收附件简历 PDF 归档 → 状态记账」的招聘闭环；HR 只在一个网页看板（OutIn HR Portal）上复核结果。

> 大白话类比：它像一支"一个人的招聘外包班组"——
> - **打铃的工头**（Windows 任务计划 + `run_round.py`）：每 2 分钟喊一次"开工"，判断是否在工作时间、是否轮到下一个人干活（全局锁，防抢手机）；
> - **大脑兼总指挥**（Pi 内核，一个外部 AI 代理，DeepSeek v4 flash 驱动）：自己不会直接碰数据库，靠两只"手"干活；
> - **左手**（mobile-mcp）：操作手机——看屏幕（dump UI）、点按钮、打字；
> - **右手**（hr-tools，26 个确定性工具）：像一位"只按规矩办事的账房先生"——建档、改状态、记账、评分、检查"这人是不是已经发过消息了"；它不做任何决策，决策全归大脑；
> - 后面还有**档案库**（MySQL + MinIO）和给 HR 看的**大屏**（hr-console）。

## 二、系统边界（谁在内、谁在外）

**系统内（hr_workbuddy，本仓库交付物）**：轮次运行器、hr-tools MCP server、pipeline 服务、screening 服务、hr-console 前端、MySQL / MinIO / Redis 数据底座。

**系统外（外部依赖）**：
- Pi 内核（`@earendil-works/pi-coding-agent`，npm 全局安装，配置在 `~/.pi/agent/`）；
- mobile-mcp（外部 npm 包，`npx mobile-mcp`，经 ADB 控制安卓设备）；
- 猎聘 App 企业版（`com.lietou.mishu`）——真实业务平台，**所有读取和触达都发生在手机端**；
- DeepSeek API（Pi 的推理大脑 + screening 的评分 LLM）；
- Windows 任务计划程序（外部调度，当前有 `hr-pi-main` 每 2 分钟、`hr-pi-daily` 每天 19:50 两条任务）。

**唯一人类用户**：HR 运营（在门户上做复核、处理转人工队列、维护岗位评分偏好）。

## 三、两代架构的演进（理解现状的关键）

这个仓库经历过一次**架构大转向**，两份"历史文档"和现在的代码说的是两个时代，先分清再往下看：

| | 第一代（M1/M2，spec v1.6 时代） | 第二代（B 方案，2026-10-09/10 起，现状） |
|---|---|---|
| 操作对象 | 电脑桌面 Chrome 上的猎聘后台 | **手机 App**（真实安卓机） |
| 执行者 | 自研 `cua-agent` arq worker（CUA SDK + 视觉大脑截图验证） | **Pi AI 代理**（现成的编码代理产品）+ MCP 工具 |
| 调度 | `scheduler` 服务（APScheduler 进程内六轮次） | Windows 任务计划 → `run_round.py` 逐轮唤醒 Pi |
| 任务下发 | Redis arq 队列 + `AtomicTask` 回调（`/internal/*`） | **无队列**：Pi 经 hr-tools 直写数据（回调端点已删） |
| 前端 | 无 | hr-console（OutIn HR Portal，Next.js + antd） |

转向的直接证据：commit `aeacfe5`（2026-10-10，"Pi 全权内核重构"）整体删除了 `services/cua-agent/`（含 1273 行的 CUA 驱动）、`services/scheduler/`、旧 spec 文档、M1 E2E 脚本；`pipeline/app/main.py` 注释写明「B 方案：内部回调端点已随旧编排层删除——执行由 Pi 内核经 hr-tools 直写」。

> ⚠️ 因此：仓库根的 `AGENTS.md`、`README.md`、`docs/runbook.md` **仍按第一代架构书写**（会提到已不存在的 cua-agent/scheduler 服务）。当前事实以 `agent/system-prompt.md`、`agent/rounds.md` 和代码为准。

另有一个遗留物：`.claude/worktrees/cdp-typing` 是 CDP 时代的并行工作区（分支 `worktree-cdp-typing`，2026-10-09 的 Windows 桌面键鼠校准成果），其提交**已全部并入 main**，工作区只是没删。

## 四、当前架构分层

```
┌─ 调度层 ──────────────────────────────────────────────┐
│ Windows 任务计划：hr-pi-main(2min) / hr-pi-daily(19:50) │
│   └─ run_round.py：窗口门(08:00-20:00)·全局锁·空轮退避     │
│        ·token 落账                                      │
└──────────────┬────────────────────────────────────────┘
               ▼  pi -p（系统提示 + 轮次指令）
┌─ 认知执行层（外部） ────────────────────────────────────┐
│ Pi AI 代理（DeepSeek v4 flash）—— 唯一决策者             │
│   ├─ mobile-mcp（9 工具）：dump UI/tap/type/…  —ADB→ 手机 │
│   └─ hr-tools（26 工具）：建档/状态机/闸门/评分/归档/记账   │
└──────────────┬────────────────────────────────────────┘
               ▼（hr-tools 进程内直连）
┌─ 业务数据层 ────────────────────────────────────────────┐
│ pipeline(FastAPI:8000)：MySQL/MinIO 唯一写入口、Alembic  │
│   schema owner、/api/jobs + /api/hr/*                   │
│ screening(FastAPI:8001)：硬规则 → LLM 评星(1-5) → 降级    │
│ 数据底座：MySQL 8.4 · MinIO · Redis 7（compose）          │
└──────────────┬────────────────────────────────────────┘
               ▼
┌─ 展示层 ────────────────────────────────────────────────┐
│ hr-console（OutIn HR Portal）：漏斗/候选人/评分/日志/人工队列 │
└─────────────────────────────────────────────────────────┘
```

**给 Java 背景的快速对照**：pipeline/screening ≈ 两个 Spring Boot 单体服务；SQLAlchemy ≈ JPA/MyBatis，Alembic ≈ Flyway；pydantic 契约包（`packages/contracts`）≈ 共享 DTO 模块；uv workspace ≈ Maven 多模块；MCP ≈ 给 AI 用的"USB 标准插头"（任何 AI 都能插上调用工具，不用为它写专用 SDK）。

## 五、各组件职责

| 组件 | 位置 | 职责（当前态） |
|---|---|---|
| 轮次运行器 | `agent/run_round.py` | 外部定时器逐轮调用；组装 system-prompt + rounds.md 作为指令，启动 `pi -p`；轮次全局锁（文件+PID 存活）；空轮退避（连续 2 空轮暂停 30 分钟）；从 Pi session 汇总 token/成本 |
| Pi 系统提示 / 轮次手册 | `agent/system-prompt.md`、`agent/rounds.md` | 业务规则的"活文档"：工具清单、7 条铁律、数据契约、标准流程、三个轮次（main/daily/backfill）的逐步指令 |
| hr-tools MCP | `services/pipeline/tools_mcp_server.py`（stdin/stdout MCP） | 26 个确定性工具。**复用 pipeline 的 models/state_machine/messaging/storage 模块**；自己持 Redis（限频桶、熔断键）、直连 MySQL、MinIO 归档、调 screening；`gated_tap` 通过闸门后**直接发 adb 点击** |
| pipeline | `services/pipeline` | FastAPI :8000。启动即 `alembic upgrade head`；对外 `/api/jobs`（岗位 CRUD 最小面）+ `/api/hr/*`（门户只读 + review/ack/PATCH 三类动作）；MySQL/MinIO 的唯一写入口 |
| screening | `services/screening` | FastAPI :8001。`POST /screen`：硬规则短路 →（`llm_scoring=false` 时直接 pass）→ LLM 按 `min_stars` 阈值评星 1-5 → LLM 不可用返回 degraded（转 deferred 重判） |
| hr-console | `apps/hr-console` | Next.js「OutIn HR Portal」：首页漏斗概览、candidates（候选人/详情）、scoring（评分）、logs（日志）、manual（人工队列）、alert-bar 告警条 |
| 数据底座 | `infra/docker-compose.yml` | MySQL 8.4（默认 profile 不启用，因本机 3306 被占用）+ MinIO（含一次性建桶容器）+ Redis 7 |

## 六、业务闭环（以 inbound 主动咨询者为例）

完整图见 [inbound-business-flow.dot](./inbound-business-flow.dot)，主线是：

1. **触发**：`hr-pi-main` 每 2 分钟叫一次 `run_round.py` → 窗口内、拿到锁 → 启动 Pi；
2. **读**：Pi 用 mobile-mcp 打开猎聘 → 消息页 → "未读" tab → 逐个会话（单轮 ≤10）：dump 提取简历 7 字段；
3. **建档**：`candidate_get_or_create`（时间戳 ID）→ 快照 → 按"沟通职位"匹配岗位（jc）；
4. **判定**：风控熔断检查（`risk_check`）→ `screen` 评星；
5. **分支一（已带附件简历）**：头像 → 资料页 → 附件简历 → PDF 查看器 → `download_attachment` 归档**真实 PDF** → 记 `interaction_log(in, attachment)` → 状态推进 `receive_resume`。**全程零触达**；
6. **分支二（无附件）**：一人一消息双查 → `gated_tap(touch=True)` 索要简历（响应性触达，不计每日 100）→ 批量 dump 验证上屏 → `interaction_log(out)` → 状态推进；
7. **收尾**：`tasklog_add` 记账 → 轮末输出 `@@RESULT@@ inbound=N outbound=M`。

其他两个轮次：`daily_round`（19:50 收尾：72h 关闭、追附件、登录检查、配额对账、延期重判、新职位检测）和 `backfill_round`（历史截图误判 → 补下载真实 PDF，疑为手动触发）。

## 七、数据模型（6 张表 + 11 态状态机）

- **jobs**：岗位（title / jd_text / hard_rules / template_msgs / scoring_prefs / llm_threshold），多职位支持是 2026-10-11 加入的；
- **candidates**：候选人（`liepin_user_id` 唯一，时间戳 ID；`online_resume_minimal` 7 字段 JSON；`snapshot_object_key` / `resume_html_key` / **`attachment_keys`（附件归档证据，防"在线简历冒充附件"）**；source=inbound|recommended）；
- **job_candidate（jc）**：候选人在某岗位下的关系（match_score / judge_reason / status / minio_object_key / resume_requested_at 等）——状态机和"一人一消息"都挂在 jc 上；
- **interactions**：互动流水（direction=in|out；msg_type=greet_request|reply|attachment|direct_request）——审计与"发过即拒"的依据；
- **review_overrides**：HR 复核推翻流水；
- **task_logs**：任务成本落账（tokens / cost / duration；`failed_needs_manual` 即转人工队列）。

**11 态状态机**（`hr_workbuddy.CandidateStatus`，状态字段以 VARCHAR 存储，允许演进）：new → screened_pass / rejected_hard / rejected_llm → greeted → resume_requested → awaiting_resume → resume_received → hr_reviewed → closed（外加 no_response）。

## 八、关键机制与安全设计（本系统的灵魂）

这套系统是"真的会碰到真人候选人"的，所以安全闸门不是点缀，而是架构主线：

1. **一人一消息**：发消息前必须双重查（jc 级 `ensure_one_message` + 候选人级 `candidate_has_out_message`）；发送后任何失败 → 只标 `failed_needs_manual` 转人工，**绝不自动重试防重发**；
2. **gated_tap 双闸门**：所有"会真实触达候选人"的点击（立即沟通/发送/索要）必须走 `gated_tap(touch=True)`，服务端先过**风控熔断 + 每日限频**两道硬闸，不通过则**物理上点不出去**（工具直接发 adb tap，返回 `executed=false` 时必须停，禁止换工具绕过）；
3. **附件证据守卫**：只有真实归档证据（`attachment_keys`）才允许推进 `receive_resume`——"看简历"打开的**在线简历截图不算附件**，防止误判；
4. **顺序铁律**：触达 → 验证上屏 → `interaction_log(out)` → `state_transition` → `tasklog_add`，顺序颠倒会被工具拒绝；
5. **风控即停**：页面出现"账号行为异常/安全中心/验证码"→ 立即停止全部操作 → `risk_set()` 写全局熔断键 → 转人工，绝不尝试绕过；
6. **串行与节流**：全系统只有**一部手机**——轮次全局锁保证同一时刻只有一个轮次；连续手机动作之间保持 1 秒（触发风控则回退 2-3 秒）；
7. **成本可见**：每轮 Pi 的 token/费用自动落进 `task_logs`，HR 门户可看。

## 九、运行与部署

- **调度**：Windows 任务计划两条（`hr-pi-main` PT2M、`hr-pi-daily` 19:50），执行 `Python312\python.exe agent\run_round.py <round>`；
- **服务**：pipeline :8000 与 screening :8001 常驻（uvicorn）；hr-console `pnpm dev`（:3000）；
- **数据底座**：`docker compose -f infra/docker-compose.yml up -d`（MySQL 默认 profile 关闭，见 compose 注释）；
- **Pi 侧配置**：`~/.pi/agent/mcp.json` 注册两个 MCP（mobile-mcp + hr-tools），`auth.json` 存凭据（勿外传）。

## 十、仓库与目录地图（避免迷路）

```
hr_cua driver/                ← 工作区（非 git 根）
├── cua_driver/               ← git 仓库根（项目名 hr_workbuddy）
│   ├── agent/                ★ Pi 时代核心：system-prompt.md / rounds.md / run_round.py + 轮次日志与状态
│   ├── services/pipeline/    ★ FastAPI + Alembic + hr-tools MCP 源码（tools_mcp_server.py）
│   ├── services/screening/   ★ 硬规则 + LLM 评分
│   ├── apps/hr-console/      ★ OutIn HR Portal（Next.js）
│   ├── packages/contracts/   ★ hr_workbuddy 共享契约（11 态 / MinimalResume / ScreenRequest）
│   ├── infra/                ★ compose 数据底座（worlds/ 为 M1 遗留 mock 剧本）
│   ├── docs/                 runbook.md（一代运维手册，部分过时）+ architecture/（本次建模产物）
│   ├── .claude/worktrees/cdp-typing/  遗留 worktree（旧分支 checkout，勿被迷惑）
│   ├── AGENTS.md / README.md          一代时代的项目说明（部分过时）
│   └── q3.py / c.txt / tables.txt …   日常会话临时脚本与报表（未跟踪）
├── mobile/                  早期手机探测 spike：adb platform-tools + uiautomator2 probe + 截图
├── ADBKeyboard.apk          手机中文输入辅助（ADB 广播输入）
└── .claude/                 工作区级 Claude/代理权限配置（含 pi 探测痕迹）
```

## 十一、文档覆盖与风险提示（Agent 就绪度简评）

- **强项**：业务规则覆盖充分——`system-prompt.md`（铁律/契约/流程）+ `rounds.md`（逐轮次指令）合起来就是一份"可执行的产品需求文档"，回归风险点（附件误判、重复触达、风控）都有明确守卫与工具级拒绝；
- **弱项**：`AGENTS.md`/`README.md`/`runbook.md` 停留在第一代架构，**新人/AI 若先读它们会被误导**（本次取证已让证据说话，见 evidence 文档）；
- **死代码/漂移**：`task_queue.py`（arq 投递）与 `hr_api` 的「rerun」文档均为一代遗留；`AtomicTask` 契约仍被导出；
- **验证盲区**：mobile-mcp 内部实现、Pi 的模型参数、风控实际触发频率、backfill 触发方式、实际连接的 MySQL 实例（见 evidence §5 未知清单）。

## 十二、产物与维护

| 产物 | 回答的问题 |
|---|---|
| `system-model.dsl` | 系统有哪些容器、谁调用谁（C4 上下文 + 容器视图，可在 Qoder 中直接预览） |
| `inbound-business-flow.dot` | 一个候选人从进来到归档，一步步怎么走、哪里会停 |
| `system-model.evidence.md` | 每个结论的证据出处、置信度、未知与验证任务 |
| 本文件 | 人类可读的整体理解（业务语言优先） |

维护建议：结构未变时手动小改；每次改动涉及组件/依赖/流程时，按 evidence 的 sourceRefs 逐条复查后再更新图源。
