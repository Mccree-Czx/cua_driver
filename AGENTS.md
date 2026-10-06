# AGENTS.md

This file provides guidance to Qoder (qoder.com) when working with code in this repository.

## 项目概述

猎聘 HR 招聘自动化系统（spec v1.6）：CUA（Computer-Use Agent）驱动猎聘企业版后台，完成「在线简历筛选 → 自动索要简历 → PDF 归档 MinIO → 候选人状态落 MySQL」。M1（inbound 主链 + mock E2E 验收）已完成并合入 main；M2/M3/M4 未启动（M3 前端 `apps/console` 尚未创建，`pnpm-workspace.yaml` 已预留 `apps/*`）。

权威资料：

- `liepin-hr-assistant-spec-v1.6.md`（仓库根）——权威 spec
- `docs/runbook.md`——运维手册（启动顺序、限频/配额/72h/登录健康语义、常见故障表、真实模式前门禁）
- `.superpowers/sdd/2026-10-04-liepin-m1/progress.md`——M1 SDD 台账（任务简报/报告/评审 diff、裁定记录）

代码注释、docstring、测试与文档均为中文书写，新增代码请保持一致。

## 常用命令

### 环境与数据底座

```bash
uv sync --all-packages    # 必须带 --all-packages：裸 `uv sync` 会把 .venv 修剪到仅根 dev 组（丢 arq/fastapi 等成员依赖）
cp .env.example .env      # 根 .env git-ignored；真实密钥只放这里
docker compose -f infra/docker-compose.yml up -d    # MySQL 8.4 + MinIO + Redis
uv run pytest infra/tests -m infra                  # 数据底座冒烟探活
```

### 服务启动（顺序：数据底座 → pipeline → screening → scheduler → worker）

```bash
# 1) pipeline :8000（导入即 alembic upgrade head；schema 唯一 owner）
uv run uvicorn app.main:app --app-dir services/pipeline --host 127.0.0.1 --port 8000
# 2) screening :8001
uv run uvicorn app.main:app --app-dir services/screening --host 127.0.0.1 --port 8001
# 3) scheduler（APScheduler 阻塞进程）
cd services/scheduler && uv run python -m app.main
# 4) cua-agent arq worker（mock 默认；real 需交互式桌面会话；PYTHONPATH 必须显式给）
cd services/cua-agent && PYTHONPATH="$PWD" uv run arq app.worker.WorkerSettings
```

手动跑 mock worker 时须设 `CUA_WORLD_PATH`（如 `infra/worlds/m1_happy_path.json`；代码默认值 `worlds/default.json` 并不存在，E2E 脚本会显式指定）。

### 测试（按服务/包跑；仓库根单次全量 pytest 有跨服务收集冲突——`test_models.py` 重名 + `app` 包遮蔽，属既有问题）

```bash
uv run pytest packages/contracts/tests     # 纯契约，零外部依赖
uv run pytest services/screening/tests     # 零外部依赖（LLM 全 mock）
uv run pytest services/cua-agent/tests     # 全 mock（无 DB/网络/桌面）
uv run pytest services/pipeline/tests      # 需 MySQL + MinIO：测试库 hr_workbuddy_test（首次需创建并授权）
uv run pytest services/scheduler/tests     # 需 Redis（127.0.0.1:6379）
uv run pytest infra/tests -m infra         # 需 compose 栈

# 单个测试
uv run pytest services/pipeline/tests/test_state_machine.py::test_override_any_non_terminal_to_any_non_terminal -v
uv run pytest services/screening/tests -k hard_rules -v
```

首次创建 pipeline 测试库（compose 栈在跑时，用 root 凭据）：

```bash
docker compose -f infra/docker-compose.yml exec mysql \
  mysql -uroot -phr_root_dev_pw -e \
  "CREATE DATABASE IF NOT EXISTS hr_workbuddy_test; GRANT ALL ON hr_workbuddy_test.* TO 'hr_user'@'%';"
```

### M1 E2E（mock 模式）

```bash
uv run python scripts/run_m1_e2e.py     # 一条命令直跑（剧本 A 全链路 + 剧本 B 拒绝/72h 关闭）
uv run pytest tests/e2e -m e2e -v       # 同一套逻辑的 pytest 包装
```

- 前置：数据底座在跑（脚本会 `up -d` 探活）；8000/8001 端口空闲（脚本自起自停子进程，不复用外部进程）。
- 破坏性：TRUNCATE pipeline 各表、Redis FLUSHDB、删 MinIO snapshots/resumes 对象——**仅在开发库运行**。
- 失败时输出会打印子进程日志目录。

### 真实账号冒烟（runbook §4 六步；零触达优先）

```bash
uv run python services/cua-agent/scripts/smoke_real.py       # 不带 --step 只打印清单，零动作
uv run python services/cua-agent/scripts/smoke_real.py --step login
uv run python services/cua-agent/scripts/smoke_real.py --step read --candidate-liepin-id <测试候选人id>
uv run python services/cua-agent/scripts/smoke_real.py --step send --candidate-liepin-id <id> --yes   # 双开关；真实触达且不可重发
uv run python services/cua-agent/scripts/smoke_real.py --step verify
```

## 架构

### 服务拓扑与数据流

```
scheduler / pipeline ──AtomicTask JSON──► arq 队列（Redis）──► cua-agent worker（宿主机桌面）
                                                                     │ 页面动作 + 截图 verify
pipeline（:8000；MySQL/MinIO 唯一写入口）◄──HTTP 回调───────────────┘
  POST /internal/tasks/{id}/result | /artifact
  └─ 推进状态机、落账、在 commit 成功后入队后继任务
screening（:8001）◄─── pipeline 同步调用 POST /screen（不进 arq）
```

- **packages/contracts**（import 名 `hr_workbuddy`）：跨服务 pydantic 契约（`AtomicTask`/`TaskResult`/`MinimalResume`/`ScreenRequest`/`ScreeningResult`/`CandidateStatus` 11 态）、`LiepinDriver`/`BrainClient` Protocol、Redis 派发注册表（`task_registry.py`）、Lua 令牌桶（`rate_limit.py`）。改契约须核对所有消费方（见 orchestrator 与 executor 的 evidence 逐字段契约）。
- **services/pipeline**：FastAPI；管理端点 `/api/jobs` + 内部面 `/internal/*`（回调、sweeps、state/login、quota/today、state/awaiting）；状态机 `app/state_machine.py`（纯函数、终态锁定、inbound/outbound 路径消歧）；编排器 `app/orchestrator.py`（结果处理、状态推进、72h 关闭、deferred 重判）；Alembic 唯一 schema owner（模型与迁移须逐列一致，`alembic check` 守卫）。
- **services/screening**：`POST /screen` = 硬规则短路 → LLM 评分 → 阈值（默认 70）。LLM 不可用返回 degraded（`judge_reason="deferred: LLM unavailable"`，HTTP 200）；pipeline 只存快照不推进，`deferred_sweep` 每 30min 自动重判。
- **services/scheduler**：APScheduler 五轮次（inbound 5min / awaiting_resume_sweep 10min / login_health 1h / daily_quota_reconcile 1h / deferred_sweep 30min，启动即各跑首轮）；工作窗口 [08:00,20:00) 门、登录失效暂停门、日配额（240）门、巡检在途去重。轮次是纯函数 + 依赖注入（`app/rounds.py`），测试直调函数、不起 APScheduler。
- **services/cua-agent**：arq worker（`max_jobs=1` 单账号串行）；`execute_task` 流程 = 动作前延时（触达 10–60s / 读 5–15s）→ SEND_MESSAGE 触达前令牌桶权威检查（20/hr）→ 驱动动作 → 截图 → 视觉大脑 verify → artifact 上传 → result 回调。`CUA_DRIVER_MODE=mock`（FakeLiepinDriver + MockBrain + World JSON 剧本）/ `real`（CuaLiepinDriver + OpenAIBrain）。**real 驱动已完成 T12 真实账号校准（2026-10-06）：7 个页面方法全部实装并经真实冒烟/联调验证（`check_login` / `list_unread_conversations` / `open_conversation` / `read_online_resume` / `send_message` / `check_attachment` / `download_attachment`）。真实模式约束：读取/操作前须 `ensure_visible()`（遮挡/其他 Space 时 Chrome 冻结渲染；跨 Space 前置用 `open -b` + 「窗口」菜单）；`liepin_user_id` 真实来源为批量预览页的「简历编号」。内置风控防线：驱动检测到平台安全验证页（账号行为异常）立即转人工不重试；失败重试默认延后 60s、任务间隔默认 30s（`CUA_RETRY_DEFER_SECONDS` / `CUA_TASK_GAP_SECONDS`；E2E instant 自动置 0）。**
- **infra/**：compose 栈 + `infra/worlds/*.json` mock 剧本；**tests/e2e/**：假 screening 服务 + E2E 包装。

### 跨文件才能理解的关键机制

- **回调路由靠派发注册表**：`TaskResult` 不含任务类型 → pipeline 用 `task_id` 从 Redis `pipeline:dispatched:{task_id}`（TTL 72h）取回派发时登记的 `AtomicTask`。scheduler 与 pipeline 必须写同一注册表（`hr_workbuddy.task_registry.write_task`）；`pipeline:dispatched:idx:{type}:{jc_id}`（TTL 1h）供 scheduler 巡检去重。
- **arq 契约**：job 函数名 `execute_task` 在 `pipeline/app/task_queue.py` 与 `cua-agent/app/worker.py` 两处常量必须逐字一致；`ArqRedis` 必须 `from_url`（位置传 DSN 会炸，arq 0.28 行为）；`max_tries=4`（1 初跑 + 3 重试），worker 折算 attempt = `task.attempt + job_try - 1`；令牌桶耗尽 / 大脑不可用经 `_defer_by` 重排（不烧重试预算、不回调结果）。
- **入队后置 + 幂等回调**：pipeline 只在 `session.commit()` 成功后入队后继任务（commit 失败 = 零副作用）；结果回调 at-least-once，重复重放安全（TaskLog 按 (task_id, attempt) 去重；各 handler 有状态检查）。
- **安全不变式**（改动 send 链前必读）：① 一人一消息（决策 3）——每 job_candidate 终身至多 1 条 out，由 `ensure_no_out_message` + worker post_send 失败处理（发送后任何失败 → `failed_needs_manual`，不自动重试、防重发）双层保障；② 延时、20/hr 令牌桶、工作窗口是风控约束，不是可选项；`CUA_E2E_INSTANT=1`（延时置 0 + 桶直通）**仅 E2E**，真实冒烟/生产严禁；③ 真实模式操作前先读 runbook §6 已知限制（另有「终身一条消息」跨岗位去重未实现，M2 待补）。
- **本机代理陷阱**：`HTTP_PROXY/HTTPS_PROXY` 会经 httpx `trust_env` 把 127.0.0.1 内网回调交给代理（特征：同连接首请求 200、后续全 404）。服务侧内部调用已 `trust_env=False`；自建脚本请设 `NO_PROXY=127.0.0.1,localhost,::1`。
- **测试导入约定**：pipeline/screening/cua-agent 的包名都是顶层 `app`——测试 conftest 把服务目录插入 `sys.path`；scheduler 的 app 包经 importlib 以别名 `scheduler_app` 注册（测试 import `scheduler_app.*`，E2E 脚本同款）；pipeline conftest 在任何 `app.*` 导入**之前**把 `DATABASE_URL` 指向测试库。
- **.env 关键项**：`CUA_DRIVER_MODE` 有效值仅 `mock|real`（旧值 `local` 会触发 pydantic 校验错误）；`CUA_BRAIN_MODEL` 必须支持 image 输入（`deepseek-flash`；`deepseek-v4-pro` 纯文本不可用）；`CUA_BRAIN_PRICE_PER_1K_TOKENS` 为成本占位待校准；`LLM_DEFAULT_THRESHOLD`/`RESUME_TIMEOUT_HOURS` 当前无代码消费（阈值 70 与 72h 为代码内定值）。
