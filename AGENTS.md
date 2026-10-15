# AGENTS.md

本文件为在此仓库工作的 AI 编码助手（Qoder、Claude Code 等）提供指引。代码注释、docstring、测试与文档均为中文书写，新增内容请保持一致。

## 项目概述

猎聘 HR 招聘自动化系统（**B 方案，第二代架构**）：外部 AI 代理 **Pi**（DeepSeek）直接操作一台**真实安卓手机**上的猎聘「招聘端」App（`com.lietou.mishu`），替 HR 完成「筛选候选人 → 打招呼 / 索要简历 → 收附件简历（真实 PDF）归档 → 状态记账」的招聘闭环；HR 在网页看板（OutIn HR Portal）上复核。

三台机器分工（当前部署形态）：

| 机器 | 跑什么 |
|---|---|
| Mac | `agent/` 脚本 + adb + 真手机；launchd 三个任务拉起轮次（main 每 300s / 登录轮询每 20s / daily 每天 19:50） |
| CT104（10.99.40.99） | MySQL / Redis / MinIO + `services/pipeline`（:8000）+ `services/screening`（:8001）+ `apps/hr-console`（:3000），systemd 常驻 |
| 开发机（Windows） | 编码、测试、git push；可本地起 compose 栈 + 服务做开发验证 |

CT104 连不到 Mac（Mac 在 NAT 后）——两边只靠 **Redis** 见面（登录请求、配额桶、风控熔断）。

> ⚠️ **第一代架构已整体删除，不要按它补代码**：`services/cua-agent`（桌面 Chrome + CUA SDK）、`services/scheduler`（APScheduler）、arq 队列与 HTTP 回调编排，随 commit `aeacfe5`（2026-10-10「Pi 全权内核重构」）一起删除；`services/` 下现在**只有** `pipeline` 与 `screening`。仓库内仍有个别第一代残留（`.env.example` 的一批 `CUA_*`/窗口/配额键、`scripts/deploy/run_service.sh` 的 scheduler/worker 分支、`packages/contracts` 的 `AtomicTask`/`task_registry`、`hr_api`/`task_queue` 的个别 docstring）——它们**没有消费者**，改动时以代码实际引用为准，不要顺手"修复"或复活。

## 权威资料（改行为前按序读）

1. **`agent/system-prompt.md` + `agent/rounds.md`**——Pi 的实际提示词：工具清单、铁律、数据契约、各轮次指令。它们描述的必然是当前行为；**改业务流程要先改它们**（提示词即产品）。
2. 代码本身：`agent/*.py`、`services/pipeline/`、`services/screening/`、`apps/hr-console/`。
3. `docs/architecture/` 四份建模产物（成文 2026-10-14；**调度一节写的是当时的 Windows 任务计划，已被 Mac launchd 取代**，其余基本准确）。
4. `services/pipeline/app/models.py`——数据模型（六张业务表 + `task_logs`）。

## 常用命令

### 环境

```bash
uv sync --all-packages --all-groups   # workspace 成员：packages/contracts + services/{pipeline,screening}；裸 uv sync 会修剪掉成员依赖
docker compose -f infra/docker-compose.yml --profile docker-mysql up -d   # 开发机数据底座；MySQL 带 profile，裸 up 不启 MySQL
uv run pytest infra/tests -m infra    # 数据底座冒烟（需 compose 栈在跑）
corepack pnpm install                 # 前端；pnpm 必须走 corepack（根 package.json pin 了 11.7.0，裸 pnpm 会版本不符直接报错）
```

### 测试（在各自 service 目录跑；仓库根单跑 pytest 有跨服务收集冲突——`app` 包遮蔽）

```bash
cd packages/contracts && uv run pytest     # 零外部依赖
cd services/screening && uv run pytest     # 零外部依赖（LLM 全 mock）
cd services/pipeline && uv run pytest      # 需 MySQL + MinIO（测试库 hr_workbuddy_test）
#   ↑ 含 hr-tools（test_tools_mcp）· 职位落库（test_job_sync/test_job_scan）· 登录状态机/轮询器（test_login_flow/test_login_watch）
```

首次为 pipeline 测试建库（compose 的 MySQL 容器在跑时）：

```bash
docker compose -f infra/docker-compose.yml exec mysql \
  mysql -uroot -phr_root_dev_pw -e \
  "CREATE DATABASE IF NOT EXISTS hr_workbuddy_test; GRANT ALL ON hr_workbuddy_test.* TO 'hr_user'@'%';"
```

### 手动起服务（开发机）

```bash
# pipeline :8000（启动即 alembic upgrade head，幂等；schema 唯一 owner）
uv run uvicorn app.main:app --app-dir services/pipeline --host 127.0.0.1 --port 8000
# screening :8001
uv run uvicorn app.main:app --app-dir services/screening --host 127.0.0.1 --port 8001
# hr-console :3000（/api/* 经 next.config rewrites 代理到 127.0.0.1:8000）
cd apps/hr-console && corepack pnpm dev
```

### Mac 侧轮次（真实手机；改 `agent/` 代码后在 Mac 上跑）

```bash
python3 agent/run_round.py main_round   # 闸门不过会打印 [skip] 并退出 0；backfill_round/login_round 不吃工作窗口
python3 agent/login_watch.py            # 登录轮询器；launchd 每 20s 拉起一次，手动跑一次同效
python3 agent/probe_jobs.py             # 职位管理页探查（**只报告不主动点**，要动必须显式 --tap x,y）
```

## 架构（当前）

### 数据流

```
Mac：Pi（deepseek-flash）
      ├─ mobile-mcp（9 工具，dump UI / tap / type，经 adb）──► 手机猎聘 App
      └─ hr-tools（stdio MCP，services/pipeline/tools_mcp_server.py）
            ├─ SQLAlchemy 直连 ──► MySQL（CT104）
            ├─ put/get ──────────► MinIO（CT104）
            ├─ Redis 客户端 ─────► 配额桶 / 风控键 / 登录请求（CT104）
            └─ HTTP POST /screen ► screening(:8001) 评分（CT104）
CT104：hr-console(:3000) ──/api/*──► pipeline(:8000) ──► MySQL / MinIO
```

- **Pi 是唯一决策者，不直连数据库**：状态与数据一律经 hr-tools 工具读写。hr-tools 复用 pipeline 的 `models`/`state_machine`/`messaging` 模块（import，非 HTTP），自带 Redis 客户端、直连 MySQL/MinIO，并在 `screen` 工具里调 screening 评分。
- **调度**：Mac 上 launchd 三个任务——`com.hr-workbuddy.pi-main`（每 300s，`main_round`）、`com.hr-workbuddy.pi-login`（每 20s，登录轮询）、`com.hr-workbuddy.pi-daily`（每天 19:50，`daily_round`）。`run_round.py` 的闸门顺序固定：**工作窗口 08:00–20:00 → 登录在途哨兵（`.login_in_flight`）→ 无 active 职位 → 空未读退避 → 全局轮次锁（`.round.lock`）**。
- **登录/换号不经 LLM**：`login_watch.py` 读 Redis 里的登录请求，跑 `agent/flows.py` 的确定性序列（`phone.py` 提供 adb 原语；UI 锚点全部外部化在 `agent/login_map.json`，**失配即停、报告屏上所见**）。换号成功后自动 `scan_jobs()` 扫在招职位 + `fetch_jds()` 抓 JD——**抓不到 JD 的职位落 `draft`**（或 JD 未确认读完整），`draft` 在工具层（`job_list_active`/`job_quota_status` 硬过滤）不可见，不会参与打招呼。
- **多账号**：`accounts` 表 + `is_active`；**所有数据按 `account_id` 隔离**（每个查询都要显式过滤——`hr_api` 的所有端点与 hr-tools 的每个工具都是这套过滤；漏一处就是跨账号数据泄露）。换号**只能**走 `POST /api/hr/accounts/{id}/switch`（真的会去手机上退旧号、登新号，成功后才改 `is_active`）；只改库不碰手机的 `activate` 类端点已删除，不要再加回来。
- **数据模型**：`accounts` / `jobs` / `candidates` / `job_candidate` / `interactions` / `review_overrides` + `task_logs`。迁移在 `services/pipeline/alembic/`，**当前 head `0012`**；`alembic check` 由 `test_migrations.py` 守卫——模型与迁移必须逐列一致（含 server_default）。
- **状态机 11 态**：`packages/contracts` 的 `CandidateStatus`，实现 `services/pipeline/app/state_machine.py`（纯函数、终态锁定、inbound/recommended 路径消歧）：new → screened_pass / rejected_hard / rejected_llm → greeted → resume_requested → awaiting_resume → resume_received → hr_reviewed → closed（+ no_response）。

### 跨文件才能理解的关键约定

- **联锁写在代码里，不写在提示词里**：提示词负责"告诉 Pi 怎么做"，**工具层负责"做错就物理失败"**。改动安全相关行为时两处都要动，且以代码里的拒绝为准（例如：无附件证据时 `state_transition(receive_resume)` / `interaction_log(attachment)` 直接报 error；触达类事件（greet/request_resume/await_resume）要求该 jc 已有 out 流水）。
- **触达点击必须走 `gated_tap(touch=True)`**：服务端先过风控熔断 + 每日 100 次桶（`counts_daily`），不过则**不执行 tap**。任何绕过（用 mobile-mcp 的 `mobile_tap` 直接点）都是安全红线级别的问题。
- **一人一消息**：jc 级 `ensure_one_message` + 候选人级 `candidate_has_out_message` 双查；`interaction_log(out)` 拒绝第二条 out。
- **顺序铁律**：触达 → 验证上屏 → `interaction_log` → `state_transition` → `tasklog_add`。
- **CWD 陷阱（改脚本前必读）**：`app/config.py` 的 `env_file=".env"` 按 **CWD** 解析。任何在 `agent/` 目录下跑又要 import `app.*` 的脚本必须先 `os.chdir(REPO_ROOT)`（`login_watch.py`、`tools_mcp_server.py` 已这么做）；在 CT104 从 `services/pipeline` 跑 alembic/脚本时要先 `export DATABASE_URL=...`，否则**静默**退回开发库默认凭据（症状：`Access denied for user 'hr_user'@'127.0.0.1'`）。
- **代理陷阱**：`HTTP_PROXY/HTTPS_PROXY` 经 httpx `trust_env` 会把 127.0.0.1 内网调用交给代理（症状：同一连接首请求 200、其后全 404）。服务侧内网调用已 `trust_env=False`；自建脚本请设 `NO_PROXY=127.0.0.1,localhost,::1`。
- **`agent/` 只允许标准库**：`run_round.py` / `phone.py` / `flows.py` 跑在 Mac 的裸 python3 上（launchd 拉起时 PATH 里未必有 Homebrew），不要引入第三方依赖；外部可执行文件（pi / node / adb）一律「候选路径探测 + 环境变量覆盖」（`HR_PI_CLI` / `HR_NODE` / `HR_ADB`），**不要赌 PATH**。
- **Pi 会话与成本**：Pi 配置目录默认 `~/.pi/agent`（`HR_PI_AGENT_DIR` 可隔离，同机多 Pi 实例必须隔离）；每轮结束 `run_round.py` 从 Pi session jsonl 汇总 tokens/cost 写一条 `task_logs`——`outcome` 按真实退出码（success/failed/timeout），`account_id` 必须带上（否则 `/api/hr/tasklogs` 按账号过滤后前端看不见）。
- **危险页与危险按钮**（真机操作）：猎聘「发职位」空白表单误提交 = 真的对外发布职位（`flows._guard_publish_form` 守卫）；职位详情页的「关闭职位」「编辑职位」不碰（编辑会触发「邀请应聘」服务终止提示）——读 JD 只走详情页本身，纯只读。
- **测试导入约定**：pipeline/screening 的包名都是顶层 `app`——测试必须在服务目录里跑；`services/pipeline/tests/test_login_watch.py` 会把 `agent/` 插进 `sys.path` 直接 import `login_watch`/`run_round`（重构这两个脚本时注意保持可导入）。

### 环境变量

实际被消费的变量（另见 `docs/runbook.md` §3.4）：

- pipeline：`DATABASE_URL` / `REDIS_URL` / `MINIO_ENDPOINT` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` / `MINIO_BUCKET` / `SCREENING_URL` / `ACCOUNT_SECRET_KEY`（Fernet 主密钥，留空则建账号 503）
- screening：`SCREENING_LLM_BASE_URL` / `SCREENING_LLM_API_KEY` / `SCREENING_LLM_MODEL`
- agent：`SCREENING_LLM_API_KEY`（run_round 读，也回退读根 `.env`）；`HR_PI_AGENT_DIR` / `HR_PI_CLI` / `HR_NODE` / `HR_ADB` / `HR_JD_MAX_JOBS` / `HR_AGREE_TERMS`

`.env.example` 中的 `CUA_*`、`WORK_WINDOW_*`、`MSG_RATE_PER_HOUR`、`DAILY_MSG_CAP`、`LLM_DEFAULT_THRESHOLD`、`RESUME_TIMEOUT_HOURS` 等**当前无代码消费**——工作窗口 08:00–20:00 与每日 100 次上限写死在代码里（`run_round.py` / `tools_mcp_server.DAILY_TOUCH_CAP`）。

## 安全红线（改代码前必读）

1. 触达（打招呼/发消息/索要简历）只能经 `gated_tap(touch=True)`；风控/验证页出现即停并 `risk_set()`，**绝不尝试绕过**。
2. 「一人一消息」是这套系统守得最死的一条——任何自动重试/补发路径都要先过双查，发送后失败一律转人工，不自动重发。
3. 不代用户做法律同意（`flows` 的「同意并继续」默认关闭，需 `HR_AGREE_TERMS` 显式开启）；不碰「注销账号」（`logout()` 有相邻坐标安全复核）。
4. 真机操作遵循 `probe_jobs.py` 的原则：**人决定点哪，机器只负责看**；脚本不得主动试错点击。
5. 历史遗留物没有明确要求不要碰：`.claude/worktrees/`（旧 worktree，提交已并入 main）不要动；第一代残留（见「项目概述」）不要顺手清理或"复活"——清理是独立任务。
