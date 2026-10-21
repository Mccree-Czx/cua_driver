# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

代码注释、docstring、测试、文档一律**中文书写**，新增内容保持一致。

## ★ 先读这条：仓库里有两套系统，外加三台执行端

`AGENTS.md` / `README.md` / `docs/architecture/` **只写了猎聘那一套**（成文较早）。仓库现在实际有**两套并行的招聘自动化系统**，BOSS 那套是 2026-10-15 之后新建的，上述三份文件均未覆盖（例如「`services/` 下只有 pipeline 与 screening」这句已不成立）。

| | 猎聘（第一代之后的 B 方案） | BOSS直聘（当前活跃开发） |
|---|---|---|
| 目标 App | `com.lietou.mishu` | `com.hpbr.bosszhipin` |
| 执行端 | [`agent/`](agent/) — **Pi（LLM）实时决定每一步** | [`agent_boss/`](agent_boss/) — **默认 Pi（LLM）内核，`BOSS_USE_PI=0` 回退确定性 `flows.*`** |
| 服务 | `services/pipeline` :8000 · `services/screening` :8001 | `services/boss-pipeline` :8100 |
| 前端 | `apps/hr-console` :3000 | `apps/boss-console` :3200 |
| 数据库 | `hr_workbuddy` · Redis **db 0** | `boss_workbuddy` · Redis **db 1** |
| Alembic head | `0013` | `0003` |
| launchd 前缀 | `com.hr-workbuddy.*` | `com.outin.boss-round-*` |
| 轮次间隔 | main 300s / login 20s / daily 19:50 | main **900s** / login 120s / daily 19:50 |
| 工作窗口 | 08:00–23:00（写死在代码里） | 08:00–23:00 |

**两套驱动的是同一部真机**（`10.99.22.219`）。同时跑会互相抢屏：一套把手机切走，另一套就抛 `FlowMismatch`，然后两边无限互相打断。

> **猎聘在真机上的三个 launchd 任务现在是停着的，而且要保持停着。** 端口/库/Redis db/launchd 前缀全部刻意错开，就是为了防止一次敲错命令打到另一套。

### 第三个执行端：云机（.66）

猎聘还有一条**云机**执行线：`10.99.40.66` 上跑多多云客户端（`ddyun.exe`），里面是一台云手机（伪装 Redmi 2201116SC，720x1280），经 adb TCP 暴露成本机端点。**云机化是为了把真机完全让给 BOSS**，解开两套抢一部手机的死结。

- 代码侧：`agent/run_round.py` 的 `ensure_device()` 第 2 档（`HR_CLOUD_PHONE=1`）→ 端口动态，找不到就拒跑不猜
- 锚点表：[`agent/login_map.cloud.json`](agent/login_map.cloud.json)，与真机那份**互不通用**（分辨率 720x1280、App 版本不同、`text` 恒为空内容全在 `content-desc`）
- 启动与调度：[`scripts/cloud/README.md`](scripts/cloud/README.md)（**必读**，`ANDROID_HOME` 那条坑就在里面）
- ★ **云机上登录/换号不可用**：「登录」「退出」两节是 `★未探`，`flows.py` 会 KeyError（刻意）。所以**不要给 `login_watch.py` 建计划任务**

## 权威资料（按需取，别只信 AGENTS.md）

| 要改什么 | 先读 |
|---|---|
| 猎聘业务流程 | [`agent/system-prompt.md`](agent/system-prompt.md) + [`agent/rounds.md`](agent/rounds.md)——**提示词即产品，改流程先改它们** |
| 猎聘运维 / 故障表 / 环境变量 | [`docs/runbook.md`](docs/runbook.md)（§3.4 是实际被消费的变量表） |
| 云机执行端（.66） | [`scripts/cloud/README.md`](scripts/cloud/README.md) |
| BOSS 的部署、常驻、调度与踩坑 | [`scripts/boss/README.md`](scripts/boss/README.md) |
| BOSS App 逆向 / 锚点策略 / 风控关卡 | [`docs/boss-app-reverse.md`](docs/boss-app-reverse.md) |
| BOSS 发职位（人工步骤与字段映射） | [`docs/boss-job-publish-guide.md`](docs/boss-job-publish-guide.md) |
| 数据模型 | `services/pipeline/app/models.py` · `services/boss-pipeline/app/models.py` |
| 共享契约（状态机枚举等） | [`packages/contracts/`](packages/contracts/)——**BOSS 复用猎聘的 `CandidateStatus`，不另立一份** |

`docs/architecture/` 是猎聘侧的建模产物（2026-10-14）；其中**调度一节写的是当时的 Windows 任务计划，已被 Mac launchd 取代**，其余基本准确。

`agent_boss/` 下**有 system-prompt.md 与 rounds.md 是正常的**——BOSS 侧默认走 Pi（LLM）内核，提示词即产品（同猎聘，改流程先改它们）；[`agent_boss/flows.py`](agent_boss/flows.py) 仍是 `BOSS_USE_PI=0` 的确定性兜底路径。

## 常用命令

### 环境（新克隆后必做）

```bash
uv sync --all-packages --all-groups   # workspace 成员：packages/contracts + services/{pipeline,screening,boss-pipeline}
                                      # ★ 裸 uv sync 会修剪掉成员依赖
docker compose -f infra/docker-compose.yml --profile docker-mysql up -d   # 裸 up 不启 MySQL
corepack pnpm install                 # 根 package.json pin 了 pnpm@11.7.0，裸 pnpm 会直接报版本不符
```

### 测试：**必须在各自 service 目录里跑**

仓库根单跑 pytest 有跨服务收集冲突（两套服务的包名都是顶层 `app`，互相遮蔽）。

```bash
cd packages/contracts      && uv run pytest    # 零外部依赖
cd services/screening      && uv run pytest    # 零外部依赖（LLM 全 mock）
cd services/boss-pipeline  && uv run pytest    # ✅ 零外部依赖：SQLite 内存库 + 内存 FakeRedis
cd services/pipeline       && uv run pytest    # ⚠️ 需 MySQL + MinIO（测试库 hr_workbuddy_test）

# 单跑一个用例
cd services/boss-pipeline && uv run pytest tests/test_state_machine.py::test_xxx
```

首次为 **pipeline** 建测试库（compose 的 MySQL 在跑时）：

```bash
docker compose -f infra/docker-compose.yml exec mysql \
  mysql -uroot -phr_root_dev_pw -e \
  "CREATE DATABASE IF NOT EXISTS hr_workbuddy_test; GRANT ALL ON hr_workbuddy_test.* TO 'hr_user'@'%';"
```

★ 在云机执行端（.66）上跑 pipeline 测试要当心：那台的 `.env` **指生产 197**。
`tests/conftest.py` 有专门的护栏（数据源非本地直接拒跑，逃生门 `HR_ALLOW_NONLOCAL_TESTS=1`），
改测试时不要绕过它。

### 手动起服务（开发机）

```bash
# 猎聘
uv run uvicorn app.main:app --app-dir services/pipeline  --host 127.0.0.1 --port 8000
uv run uvicorn app.main:app --app-dir services/screening --host 127.0.0.1 --port 8001
cd apps/hr-console && corepack pnpm dev                                    # :3000

# BOSS（端口必须错开，见上）
cd services/boss-pipeline && uv run uvicorn app.main:app --host 0.0.0.0 --port 8100
corepack pnpm --filter boss-console dev -- -p 3200   # 两个前端的 dev 默认都抢 3000，必须显式指定
```

两个 pipeline 都在**启动时跑 `alembic upgrade head`**（幂等；schema 的唯一 owner）。

### 前端

```bash
corepack pnpm --filter boss-console build    # ★ 改了 boss-console 必须重新 build
corepack pnpm --filter boss-console lint
```

### 轮次（真机 / 云机）

```bash
# 猎聘 · 真机（需 venv，它要 redis）
~/outin-hr/.venv/bin/python agent/run_round.py main_round
# 猎聘 · 云机（.66，走包装脚本，环境变量都在里面）
scripts\cloud\round.cmd backfill_round
# BOSS（纯标准库，用系统 python3；venv 坏掉不影响轮次）
python3 agent_boss/run_round.py main_round
```

闸门不过会打印 `[skip]` 并退出 0——**`[skip]` 不是故障**。

## 架构要点（跨文件才看得出）

### 数据流

```
猎聘：执行端上 Pi(LLM) ─┬─ mobile-mcp ──► 手机猎聘 App
                        └─ hr-tools(stdio MCP, services/pipeline/tools_mcp_server.py)
                                └─ SQLAlchemy/MinIO/Redis 直连 + HTTP► screening(:8001)

BOSS：Mac 上 agent_boss(默认 Pi 内核) ─┬─ mobile-mcp ──► 手机 BOSS App
                                        └─ hr-tools(stdio MCP, services/boss-pipeline/tools_mcp_server.py)
                                                └─ SQLAlchemy/MinIO/Redis 直连 + HTTP► screening(:8001)
      （BOSS_USE_PI=0 时回退 flows.* 确定性点击 + HTTP /api/ingest/* ──► boss-pipeline）
```

- **BOSS 侧默认 Pi（LLM）内核**：`run_round.py` 的 `USE_PI` 默认 1，spawn Pi 挂 mobile + hr-tools 两个 MCP 实时决定每一步；真机写动作仍走服务端闸门（gated_tap/greet_check/配额），LLM 只负责「看和决定」。逃生口 `BOSS_USE_PI=0` 退回 `flows.py` 的确定性路径（已在真机验证过的那套，走 HTTP `/api/ingest/*` 落账）。
- **猎聘侧 Pi 是唯一决策者且不直连数据库**，一切经 hr-tools 工具读写。
- **`agent/` 与 `agent_boss/` 都不是 uv workspace 成员**，靠 `sys.path` 引入；共用逻辑抽在 [`shared/android_ui.py`](shared/android_ui.py)（adb 原语 + 锚点匹配），该文件**只用标准库**，改动时守住这条。

### 锚点策略两套相反

猎聘 App 是 Flutter：几乎无稳定 ID，锚点 = **坐标 + 精确文字**（`agent/login_map.json` / `login_map.cloud.json`）。
BOSS 是原生 Android：**优先 `resource-id`**，坐标只作兜底（`agent_boss/boss_map.json`）。

**不要照抄猎聘的写法去做 BOSS 的锚点。** 锚点失配一律抛 `FlowMismatch` 并附上屏上所见——正确动作是**重新校准锚点表**，不是现场试错点击。

### 登录：BOSS 三道关卡必须人工

易盾滑块 / 短信 / 人脸实名，**三道全部无法自动化**。而关卡页长得就像「掉登录了」。所以 BOSS 用 `boss:login_gate_in_flight` 标记「手机停在关卡前等人」：轮次看到它**安静跳过**（不是失败），且**不去动那个标记**——它由人在手机上过完关卡后清除。照搬猎聘的 `login_req` 语义会把好账号误判成掉登录 → 假熔断 → 锁死。

设计范式是「**机器推到关卡前，人过卡，机器接着走**」。

## 跨文件才能理解的陷阱

- **CWD 陷阱**：`app/config.py` 的 `env_file=".env"` 按 **CWD** 解析。在 `agent*/` 下跑又要 import `app.*` 的脚本必须先进 `os.chdir(REPO_ROOT)`；在服务目录外跑 alembic 要先 `export DATABASE_URL=...`，否则**静默**退回开发库默认凭据。
- **`app.main` 在 import 期就跑迁移**：所以测试的 `conftest.py` 必须在 `import app.*` **之前**设好 `DATABASE_URL`，否则会去连真库（两套服务的 conftest 都有这条注释）。
- **代理陷阱**：`HTTP_PROXY/HTTPS_PROXY` 经 httpx `trust_env` 会把 127.0.0.1 内网调用交给代理（症状：同一连接首请求 200、其后全 404）。自建脚本设 `NO_PROXY=127.0.0.1,localhost,::1`。
- **不要赌 PATH**：外部可执行文件（adb / node / pi）一律「候选路径探测 + 环境变量覆盖」（`HR_ADB` / `HR_NODE` / `HR_PI_CLI`）。launchd 与 Windows 任务计划拉起的进程 PATH 都不完整。
- **`alembic check` 由 `test_migrations.py` 守卫**：模型与迁移必须逐列一致（含 `server_default`）。这个漂移看起来永远像小问题，但意味着「迁移建出来的库」和「模型以为的库」是两份东西。
- **改了前端没生效**：`next start` 服务的是 `.next/` 里的**预构建产物**，restart 不重建。必须 `pnpm --filter <app> build`。（猎聘侧 2026-10-15 为这条丢掉了一整天的改动。）
- **Windows 侧重启用 `schtasks /end` + `/run`，不要 `taskkill` 子进程**：实测 taskkill 掉 `next start` 会连带把另一个任务的进程也 `^C` 掉。两个 `.cmd` 包装脚本启动前先 `netstat` 查端口，是因为端口被占时新进程**静默退出**（`EADDRINUSE` 只进日志）。
- **写 `.cmd` 的两条硬规则**：① **ASCII only**——cmd 默认按 GBK 读脚本，中文注释会崩；中文说明写同目录 `README.md`。② **必须 CRLF 换行**——LF-only 的 `.cmd` 会被 cmd 解析错乱，症状是毫不相关的 `'xxx' is not recognized as an internal or external command`。两条都是实测踩出来的。
- **BOSS 的 main_round 要 415 秒**：所以 launchd 间隔是 900s 而不是照抄猎聘的 300s——按 300 排的话每轮都撞 `RoundLock` 被 `[skip]`，**看起来在跑，其实大半轮次什么都没做**。给新执行端定间隔前，先量一轮实际耗时。
- **`.env.example` 里有一批第一代遗留键**（`CUA_*` / `WORK_WINDOW_*` / `DAILY_MSG_CAP` 等）**当前无代码消费**；第一代架构（`services/cua-agent`、APScheduler、arq）已随 `aeacfe5` 整体删除。遇到残留**不要顺手清理或"复活"**，改动时以代码实际引用为准。

## 安全红线（改代码前必读）

1. **触达（打招呼/发消息/索要简历）只能经 `gated_tap(touch=True)`**，服务端先过风控熔断 + 每日配额桶，不过则不执行 tap。任何绕过（直接调 `mobile_tap`）都是红线级问题。
2. **「一人一消息」是守得最死的一条**：jc 级 + 候选人级双查，`interaction_log(out)` 拒绝第二条 out。**发送后失败一律转人工，绝不自动重发**——任何自动重试/补发路径都要先过双查。
3. **BOSS 自动打招呼默认关着**（需显式 `BOSS_GREET_ENABLED=1`）：该账号刚被风控判过异常、刚过完人脸，批量发消息风险太大。日上限 150 是**平台实测值**（每日主动向推荐人打招呼的上限，2026-10-21 用户提供）。
4. **不可逆的真机动作不进 `flows.py`**：立即沟通 / 开放职位 / 发布表单提交 / 编辑·关闭职位 / 解绑实名。发布职位**必须人工**（第二页字段未探到，且「职位名称/城市/招聘类型」发布后不可修改）。写这类流程要单独开、带授权闸门。
5. **真机操作原则**：人决定点哪，机器只负责看。脚本不得主动试错点击（`probe_jobs.py` / `probe_app.py` 只报告，要动必须显式 `--tap x,y`）。
6. **不代用户做法律同意**（猎聘「同意并继续」需 `HR_AGREE_TERMS` 显式开启）；不碰「注销账号」「解绑实名」。
7. **风控/验证页出现即停**并 `risk_set()`，**绝不尝试绕过**。

## 约定

- **`agent/` 与 `agent_boss/` 只允许标准库**：可能被裸 `python3` 拉起（launchd 的 PATH 里未必有 venv/Homebrew）。猎聘侧那个 venv 只有 redis 一个理由；**别为了图方便往 `agent_boss/` 里引第三方依赖**。
- **账号数据一律按 `account_id` 隔离**：每个查询都要显式过滤，漏一处就是跨账号数据泄露。猎聘侧换号**只能**走 `POST /api/hr/accounts/{id}/switch`（真的会去手机上退旧号、登新号），只改库的 `activate` 类端点已删除，不要再加回来。
- **顺序铁律**：触达 → 验证上屏 → `interaction_log` → `state_transition` → `tasklog_add`。
- **联锁写在代码里，不写在提示词里**：提示词负责「告诉 Pi 怎么做」，**工具层负责「做错就物理失败」**。改动安全相关行为时两处都要动，且**以代码里的拒绝为准**。
- **同一个事实不要有两个判断来源**：判断跟着锚点表/配置走，别另造一个环境变量去重复判断——两个来源迟早会不一致，而不一致就是静默做错。
- **两个前端的 `app/` 目录都用 Next.js 16.4**：这个版本相对训练数据有破坏性变更，写代码前读 `node_modules/next/dist/docs/` 里的相关指南（`apps/hr-console/AGENTS.md` 有这条提示，由 `next dev` 自动维护）。
