# hr_workbuddy 运维手册（runbook）

> 适用范围：**B 方案（第二代架构，2026-10 起）**——Pi AI 代理 + 真安卓手机 + CT104 数据面。
> 第一代架构（桌面 Chrome + `services/cua-agent` / `services/scheduler` / arq 队列 / HTTP 回调编排）已随 commit `aeacfe5`（2026-10-10）整体删除，本文档不再描述它们。
> 编码约定：标注「TODO：待确认」的内容在写稿时**未在代码/仓库中找到依据**，需要人工核实后再补，不要当成事实引用。

## 1. 架构速览

```
┌─ Mac（执行面） ────────────────────────────────────────────────┐
│ launchd 三个任务：                                             │
│   pi-main  每 300s  → run_round.py main_round   ┐             │
│   pi-login 每 20s   → login_watch.py（换号轮询） ├─ 全局轮次锁  │
│   pi-daily 19:50    → run_round.py daily_round  ┘  串行一部手机 │
│ run_round → Pi（deepseek-flash）                               │
│   ├─ mobile-mcp（9 工具）：dump UI / tap / type ──adb──► 手机   │
│   └─ hr-tools（stdio MCP，32 个确定性工具）                    │
│ login_watch → flows.py + phone.py（确定性序列，不经 LLM）       │
│   锚点外部化：agent/login_map.json                             │
└──────────────┬────────────────────────────────────────────────┘
               │ 两边只靠 Redis 见面（CT104 连不到 Mac，Mac 在 NAT 后）
┌─ CT104（数据面 + 服务面，systemd） ────────────────────────────┐
│ MySQL 8.4 · Redis 7 · MinIO                                    │
│ pipeline(:8000)：MySQL/MinIO 唯一写入口、Alembic schema owner、 │
│   /api/jobs + /api/hr/*（含账号/登录/复核/日报/人工队列）        │
│ screening(:8001)：硬规则 → LLM 1-5 星评分 → degraded 降级       │
│ hr-console(:3000)：OutIn HR Portal（/api/* 代理到 pipeline）    │
└────────────────────────────────────────────────────────────────┘
```

要点：

- **Pi 是唯一决策者，不直连数据库**：一切状态/数据经 hr-tools 工具（`services/pipeline/tools_mcp_server.py`）读写。hr-tools 复用 pipeline 的 `models`/`state_machine`/`messaging` 模块（import，非 HTTP），自带 Redis 客户端、直连 MySQL/MinIO，并在 `screen` 工具里调 screening 评分。
- **screening 是旁路调用**：由 hr-tools 的 `screen` 工具 HTTP 调用 `POST /screen`；LLM 不可用返回 degraded（`judge_reason="deferred: LLM unavailable"`，HTTP 200），候选人只存快照不推进，等 daily_round 的「延期重判」再筛。
- **Redis 用途**（键按账号分片，`services/pipeline/app/keys.py`）：登录请求 `pipeline:state:login_req`（单例 hash，TTL 30min）；登录态 `pipeline:state:login:{account_id}`；每日触达计数 `msg-touch:{account_id}:{YYYYMMDD}`；风控熔断 `cua:risk:paused:{account_id}`；另有第一代遗留的派发登记 `pipeline:dispatched:*`（只被人工队列反查读取）。
- **状态机 11 态**（`packages/contracts` 的 `CandidateStatus`）：new → screened_pass / rejected_hard / rejected_llm → greeted → resume_requested → awaiting_resume → resume_received → hr_reviewed → closed（+ no_response）。
- **多账号**：`accounts` 表 + `is_active`；**所有数据按 `account_id` 隔离**。换号只能走 `POST /api/hr/accounts/{id}/switch`（真的会去手机上退旧号、登新号）。

## 2. 部署拓扑（四台机器）

| 机器 | 地址 | 跑什么 |
|---|---|---|
| Mac | `10.99.31.81`（用户 `outin`） | 猎聘 · **真机**执行端：Pi + mobile-mcp + adb + 真手机；`agent/` 脚本；launchd 三个任务（**现停用**——真机让给了 BOSS） |
| 云机执行端（Windows） | `10.99.40.66` | 猎聘 · **云机**执行端：多多云（`ddyun.exe`）里的云手机 + `agent/` 脚本；启动与调度见 [`scripts/cloud/README.md`](../scripts/cloud/README.md) |
| CT104 | `10.99.40.99`（主机名 `openviking`） | MySQL / Redis / MinIO + pipeline:8000 + screening:8001 + hr-console:3000（全 systemd） |
| 开发机（Windows） | 本机 | 开发、测试；唯一能 push GitHub 的机器 |

其他已知事实（2026-10-14 记录，TODO：部署变更时复核）：

- 手机经 **WiFi adb over TCP** 连到 Mac（不是 USB）。
- **云机执行端（2026-10-16 新增）**：云手机的 adb 端口**是动态的**（一次会话内就会变），
  由 `run_round.ensure_device()` 现找；**不要 `adb disconnect` 云机**（会让客户端关掉端点）。
  该机的 `.env` **指生产库**，所以在那台上跑 pipeline 测试会被 `tests/conftest.py` 的
  数据源护栏拒跑（`HR_ALLOW_NONLOCAL_TESTS=1` 是逃生门，别随手开）。
- CT104 的仓库路径 `/root/outin-hr`；Mac 的仓库在 `~/outin-hr`。
- **发布流程**：开发机提交并 push → Mac `git pull` → CT104 `git pull` + `alembic upgrade head`（注意 §11 的 DATABASE_URL/CWD 坑）+ 重启 pipeline + 前端 `pnpm exec next build` + 重启 console。
- **TODO：待确认**：CT104 上各服务的 systemd 单元名清单（使用记录中出现过 `hr-pipeline`、`hr-console`；screening 与 MySQL/Redis/MinIO 的单元名未见于仓库）；Mac 上三个 `com.hr-workbuddy.pi-*` plist 的内容（仓库内没有对应安装脚本）。

## 3. 环境准备

### 3.1 Python 环境（uv workspace）

```bash
uv sync --all-packages --all-groups
```

- 裸 `uv sync` 会修剪掉成员依赖（workspace 成员：`packages/contracts` + `services/{pipeline,screening}`）；`uv run` 不触发修剪。
- 拉包慢时可用一次性镜像源 `$env:UV_DEFAULT_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"`（开发机）；uv.lock 已归一为 pypi.org，勿把镜像写进 lock。

测试（在各自 service 目录跑；仓库根单跑 pytest 有跨服务收集冲突）：

```bash
cd packages/contracts && uv run pytest     # 零外部依赖
cd services/screening && uv run pytest     # 零外部依赖（LLM 全 mock）
cd services/pipeline && uv run pytest      # 需 MySQL + MinIO（测试库 hr_workbuddy_test）
```

首次建 pipeline 测试库（compose MySQL 容器在跑时）：`docker compose -f infra/docker-compose.yml exec mysql mysql -uroot -phr_root_dev_pw -e "CREATE DATABASE IF NOT EXISTS hr_workbuddy_test; GRANT ALL ON hr_workbuddy_test.* TO 'hr_user'@'%';"`

### 3.2 数据底座

- **生产**：CT104 上的 MySQL / Redis / MinIO（systemd；单元名 TODO 待确认）。
- **开发机**：`infra/docker-compose.yml`（MySQL 8.4 + MinIO + Redis 7）。

```powershell
# 开发机起栈（MySQL 带 profile，裸 up 不启 MySQL！）
docker compose -f infra/docker-compose.yml --profile docker-mysql up -d
uv run pytest infra/tests -m infra    # 冒烟验证 MySQL / MinIO / Redis
docker compose -f infra/docker-compose.yml down        # 停栈
docker compose -f infra/docker-compose.yml down -v     # 连数据卷一起清（慎）
```

注记（实测经验）：

- 首次 `up -d --wait` 可能因 mysql 初始化自带 restart 周期超时退出 1——等约 30s 重跑即全 healthy。
- `minio-init` 是一次性容器（建 `hr-workbuddy` 桶后退出 0），部分 compose 版本 `--wait` 会把它判为失败——用 `up -d` + `infra` 冒烟测试探活最稳。
- **镜像加速**（Docker Hub 直连不通时；拉完 tag 成 compose 引用名即可）：

```powershell
docker pull docker.m.daocloud.io/library/mysql:8.4
docker tag  docker.m.daocloud.io/library/mysql:8.4 mysql:8.4
docker pull docker.m.daocloud.io/library/redis:7
docker tag  docker.m.daocloud.io/library/redis:7 redis:7
docker pull quay.m.daocloud.io/quay.io/minio/minio:latest
docker tag  quay.m.daocloud.io/quay.io/minio/minio:latest quay.io/minio/minio:latest
docker pull quay.m.daocloud.io/quay.io/minio/mc:latest
docker tag  quay.m.daocloud.io/quay.io/minio/mc:latest quay.io/minio/mc:latest
```

### 3.3 前端（pnpm 必须走 corepack）

```bash
cd apps/hr-console && corepack pnpm install
corepack pnpm dev        # 开发（:3000）
corepack pnpm build && corepack pnpm start   # 生产/常驻
```

根 `package.json` 用 `packageManager` pin 了 pnpm 版本——直接敲 `pnpm` 会因版本不符直接报错，一律走 `corepack pnpm`。

### 3.4 .env 变量表（**实际被代码消费**的）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `mysql+pymysql://hr_user:hr_dev_pw@127.0.0.1:3306/hr_workbuddy` | pipeline / 脚本读写库 |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | 登录请求 / 登录态 / 配额桶 / 风控熔断 |
| `MINIO_ENDPOINT` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | `127.0.0.1:9000` / `minioadmin` / `minioadmin` | 对象存储 |
| `MINIO_BUCKET` | `hr-workbuddy` | 归档桶（备份/报告脚本同样读它） |
| `SCREENING_URL` | `http://127.0.0.1:8001` | hr-tools 调 screening 的地址 |
| `DAILY_RESUME_TARGET` | `50` | 总览页「日回收目标」展示用（不拦任何流程） |
| `ACCOUNT_SECRET_KEY` | 空 | 账号密码 Fernet 主密钥（见 §3.5）；留空则建账号接口 503 |
| `SCREENING_LLM_BASE_URL` / `SCREENING_LLM_API_KEY` / `SCREENING_LLM_MODEL` | `https://api.deepseek.com` / 空 / `deepseek-flash` | screening 评分 LLM（缺 key 时服务照常启动、评分走 degraded） |

Mac / `agent/` 侧另读（非 .env，直接环境变量）：`SCREENING_LLM_API_KEY`（run_round 给 Pi 用；未设则回退读仓库根 `.env` 的同名键）、`HR_PI_AGENT_DIR`（Pi 配置目录，默认 `~/.pi/agent`）、`HR_PI_CLI` / `HR_NODE` / `HR_ADB`（可执行文件路径覆盖）、`HR_JD_MAX_JOBS`（每次登录后最多抓几个 JD，默认 5）、`HR_AGREE_TERMS`（默认关闭：是否代点独立协议同意页）。

**工作窗口 08:00–20:00、每日 100 次触达上限均写死在代码里**，不存在对应 env（见 §3.6）。

### 3.5 账号密码加密密钥（`ACCOUNT_SECRET_KEY`）

生成：

```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

要点：

- **建账号（`POST /api/hr/accounts`）需要它**：手机号与密码都必填，密码经 Fernet 加密后入库；密钥缺失时接口返回 503（`CredentialCryptoError`）。
- 密码在库里只有密文；接口只回 `phone_masked` 与 `has_password`，密文与明文都不外传。
- **密钥丢失 = 已存的密码不可恢复**（账号本身不受影响，重填即可）。密钥要纳入备份。
- 轮换用 `MultiFernet`（新密钥放最前，旧密文仍能解，逐个重加密后再摘掉旧密钥）。
- ⚠️ 待确认（见 §14）：登录方式目前只有短信验证码一条路（密码登录已停用，见 §7），`decrypt_secret` 目前只被测试使用——库里的密码当前没有实际消费方。

### 3.6 `.env.example` 的遗留键（不要被误导）

`.env.example` 仍列着一批**第一代架构**的键，当前**无任何代码消费**：`CUA_DRIVER_MODE`、`CUA_TASK_GAP_SECONDS`、`CUA_RETRY_DEFER_SECONDS`、`CUA_BRAIN_*`、`WORK_WINDOW_START/END`、`MSG_RATE_PER_HOUR`、`DAILY_MSG_CAP`、`LLM_DEFAULT_THRESHOLD`、`RESUME_TIMEOUT_HOURS`。工作窗口（08:00–20:00）写在 `agent/run_round.py`；每日上限（100）写在 `tools_mcp_server.DAILY_TOUCH_CAP`。脚本层同款残留：`scripts/deploy/run_service.sh` 里仍有 `scheduler`/`worker` 分支（对应服务已删除）。TODO：清理这两处，待排期。

## 4. 服务运行

### 4.1 手动启动（任一台装了依赖的机器）

```bash
# pipeline :8000（启动即 alembic upgrade head，幂等；schema 唯一 owner）
uv run uvicorn app.main:app --app-dir services/pipeline --host 127.0.0.1 --port 8000
# screening :8001（健康检查：GET /health）
uv run uvicorn app.main:app --app-dir services/screening --host 127.0.0.1 --port 8001
# hr-console :3000
cd apps/hr-console && corepack pnpm start
```

pipeline 没有 `/health`，探活用 `GET /api/hr/overview`（会读生效账号；返回 503 表示没有生效账号，见 §11）或 `/docs`。

### 4.2 Mac 侧轮次脚本（手动）

```bash
python3 agent/run_round.py main_round      # 只用标准库；闸门不过打印 [skip] 并退出 0
python3 agent/run_round.py daily_round
python3 agent/run_round.py backfill_round  # 人工触发
python3 agent/login_watch.py               # 登录轮询器单次执行（launchd 每 20s 拉起的就是它）
python3 agent/probe_jobs.py                # 职位管理页探查（只报告；要动必须显式 --tap x,y）
```

`run_round.py` 把 Pi 的 stdout 写到 `agent/.round_<name>.log`；超时：`login_round` 420s，其余 3600s。每轮结束从 Pi session jsonl 汇总 tokens/cost 写一条 `task_logs`（`outcome` 按真实退出码）。

### 4.3 常驻（现状）

- **Mac**：launchd 三个任务（`com.hr-workbuddy.pi-main` 每 300s、`com.hr-workbuddy.pi-login` 每 20s、`com.hr-workbuddy.pi-daily` 每天 19:50）。TODO：plist 内容与安装方式不在仓库内，待补。
- **CT104**：pipeline / screening / hr-console 由 systemd 常驻（单元名 TODO，见 §2）。
- 仓库内脚本：`scripts/deploy/install.sh` 会以 launchd 安装 pipeline/screening/backup 三件（2026-10-15 已删掉 scheduler/worker 分支；`uninstall.sh` 的循环里还留着两个旧名字，可忽略）。它与当前 CT104 systemd 形态的关系 TODO 待确认；`run_service.sh` 保留供手动调试。

## 5. 轮次与调度

### 5.1 `run_round.py` 闸门顺序（跳过原因对照）

| 顺序 | 闸门 | 跳过时打印 |
|---|---|---|
| 1 | 工作窗口 08:00–20:00（`backfill_round` / `login_round` 豁免） | `[skip] <name>: 工作窗口外（HH:MM）` |
| 2 | 登录在途哨兵 `.login_in_flight` 新鲜（main/daily 让路，避免把登录页误判成掉登录） | `[skip] <name>: 有登录在途（手机停在登录页，跑本轮会误判成掉登录）` |
| 3 | 当前生效账号名下**有 active 职位**（仅 main_round；**fail-open**：查不到一律放行） | `[skip] <name>: 当前账号名下没有已启用的职位（在前端补齐 JD 后自动恢复）` |
| 4 | 空未读退避（仅 main_round；连续 2 个空轮 → 30 分钟内不启动 Pi） | `[skip] <name>: 空未读退避（连续空，30 分钟内不再启动 Pi）` |
| 5 | 全局轮次锁 `.round.lock`（文件 + PID 存活；stale 3900s） | `[skip] <name>: 另一轮次正在运行（全局轮次锁）` |

注意：第 3 步**刻意不碰** `.inbound_state.json`——「没职位」是配置问题，不是「今天没活干」，记进退避会把后续 30 分钟全挡掉。

### 5.2 四个轮次

| 轮次 | 触发 | 做什么 |
|---|---|---|
| `main_round` | launchd 每 300s | 先 `quota_status()`：**remaining>0 → outbound 打招呼**（单轮 ≤35 卡，不碰未读）；**remaining==0 → inbound 处理未读**（单轮 ≤10 会话）。轮末必须输出机器可读行 `@@RESULT@@ inbound=N outbound=M`（供退避判定） |
| `daily_round` | launchd 每天 19:50 | 72h 关闭 + 追附件（只读）→ 登录检查（未登录 `risk_set()` 报告转人工 / 已登录 `risk_clear()`）→ 配额对账（只报告）→ **延期重判**（`status=new` 且 judge_reason 含 deferred 的 jc 重新 screen）→ 新职位检测（手机读职位列表，与库对比，新职位补录 JD） |
| `backfill_round` | **人工**（TODO：触发方式未定） | `list_backfill_candidates()` → 逐个补下载真实 PDF（全程零触达，单轮 ≤20） |
| `login_round` | **人工兜底**，不再自动拉起 | 确定性序列跑不通时（如猎聘改版、锚点失配）由人叫 Pi 看一眼；正常登录路径见 §7 |

### 5.3 锁 / 哨兵 / 退避文件（都在 `agent/` 下）

| 文件 | 语义 | 自愈 |
|---|---|---|
| `.round.lock` | 全局轮次锁：内容为持有 PID；有效 = 进程存活 且 mtime 新鲜（<3900s） | 崩溃残留锁因 PID 不存活自动失效 |
| `.login_in_flight` | 登录在途哨兵：`login_watch.py` 每次轮询重写；run_round 只见文件、读不了 Redis | 最长 2400s 后自动失效（防轮询器挂掉把主线永久堵死） |
| `.inbound_state.json` | 空未读计数 `{empty_streak, last_empty_ts}` | 30 分钟后自动恢复；有新产出即清零 |
| `.round_<name>.log` | 本轮 Pi 的 stdout | 每轮覆盖 |

## 6. 配额、限频与风控

### 6.1 每日 100 次与职位均分

- 每日打招呼总额 **100**（`DAILY_TOUCH_CAP`，Redis 键 `msg-touch:{account_id}:{YYYYMMDD}`，86400s 窗口）。
- **按职位均分**：每职位日配额 = `floor(100/N)`（N = 当前账号 active 职位数）。`job_quota_status()` 给出每职位今日已打数，Pi 优先给「已打 < 配额 且还有候选人」的职位打招呼；某职位满配额或候选人耗尽则让给其他职位。
- inbound 的响应性触达（回未读、索要简历）走 `gated_tap(counts_daily=False)`——**只过风控熔断，不计入每日 100**。
- 旧的 20 条/小时令牌桶与 240 条/天配额已随第一代删除，代码里没有对应闸门。

### 6.2 `gated_tap` 闸门（触达点击的唯一合法通道）

`gated_tap(x, y, touch=True)`：服务端先过 **风控熔断**；`counts_daily=True`（主动打招呼）再扣每日 100 桶。任一不过**不执行 tap**，返回 `executed=false` + 原因——此时 Pi 必须停手报告，**严禁换 `mobile_tap` 绕过**。读操作（浏览、进详情、切 tab）用 `touch=False` 直接点。

### 6.3 风控熔断（按账号）与恢复

- 键：`cua:risk:paused:{account_id}`（**按账号分片**；旧文档写成全局 `cua:risk:paused` 已过时）。
- 触发：Pi dump 到「账号行为异常 / 猎聘安全中心 / 图形验证码 / 滑块」→ 立即停止全部操作 → `risk_set()` → 报告转人工，**绝不尝试绕过**。
- 影响：该账号所有 `touch=True` 点击被拒；`risk_check()` 为 paused 时 pi 的轮次应停止触达。
- **恢复流程**：人工在手机上完成安全验证并确认页面恢复正常后 `risk_clear()`（或前端页面按 §11 的按键指引清除）。观察期内以低密度恢复（先只读，再逐步放宽）。
- 2026-10-15 实测：猎聘「账号密码登录」点登录会弹滑块验证码——这正是密码登录被停用的原因（见 §7）。

### 6.4 工作窗口

08:00–20:00（`[start, end)`，写死在 `run_round.py`）。窗口外 main/daily 直接跳过；`backfill_round`（只读）与 `login_round`（人工即时动作，晚上换号是常态）豁免。

## 7. 登录与换号（跨机器流程）

### 7.1 正常路径（不经 LLM）

```
HR 在前端点「切换账号/登录」 → pipeline 写 Redis（`pipeline:state:login_req`，kind=switch|login, phase=pending）
→ Mac 上 login_watch.py（launchd 每 20s）轮询到 → 跑 flows.py 确定性序列（phone.py adb 原语，锚点查 login_map.json）
→ kind=switch 先 flows.logout() 退出旧号，再 flows.login_code(手机号)（点「下一步」，验证码自动发出→phase=awaiting_code）
→ HR 在自己手机读到验证码，填进前端（POST …/login-request/code；TTL 5 分钟）
→ login_watch 原子取码 → flows.submit_code() → 成功：mark_account_logged_in（写登录态 + 切 is_active）
→ 自动扫该账号在招职位 sync_scanned_jobs（抓不到 JD 的落 draft）→ 抓 JD flows.fetch_jds（≤HR_JD_MAX_JOBS 个，只读详情页）
→ phase=success（前端显示：扫到/更新了多少职位、JD 抓到几个、几个已自动启用）
```

设计要点：

- **登录页没有「获取验证码」按钮**（点「下一步」验证码自动发出；实测 2026-10-15）；手机号是预填的，输入前必须清空（`phone.type_into` 已处理）。
- **密码登录已停用**（实测弹滑块）：接口对 `method=password` 直接 400；唯一可用路径是验证码。
- **登录成功 ≠ 扫职位成功**：扫描/抓 JD 失败只回一句 `notice`（例如「登录成功，但职位没扫到…」），不会把成功打成失败——重来要再消耗一条短信验证码。
- **相位 `scanning_jobs` 必须是非终态**：扫描要动手机一分钟左右，靠 `.login_in_flight` 哨兵保持新鲜，拦住 main_round 抢手机。
- **抢占**：用户当场发起的登录比定时轮次更急——若轮次正在跑，login_watch 会**硬抢占**（SIGTERM → 等 10s → SIGKILL 进程树），并写一条 `outcome=preempted` 的 `task_logs` 供事后核对（⚠️ 可能打断一次「已点出去但还没落账」的触达 → 下一轮可能重复打招呼；「一人一消息」在这条路径上无法保证，人需按 preempted 记录核对）。
- 失败时前端会给「登回 <旧号名>」入口（`from_account_id`）；换号失败且旧号已退出 = **手机上现在没有账号**，需人工介入。

### 7.2 相位表（`pipeline:state:login_req`）

| 相位 | 含义 | 谁推进 |
|---|---|---|
| `pending` | 前端发起，等 Mac 拉起 | login_watch 读到即动手 |
| `logging_out` | （仅 switch）正在退出旧号 | login_watch |
| `dispatched` | 正在操作手机（输号、发验证码） | login_watch |
| `awaiting_code` | 验证码已发出，等人工填 | HR 填码后 `submit_code` 仍回此相位，等 login_watch 取 |
| `scanning_jobs` | 登录已成，正在扫职位/抓 JD（非终态，防抢手机） | login_watch |
| `success` / `failed` / `cancelled` / `expired` | 终态 | — |

约束：整个请求 TTL 30 分钟（超时由 login_watch 置 `expired`）；自动尝试上限 `MAX_ATTEMPTS=3`（超限置 `failed`，避免无限空跑）；验证码一次性（Lua 原子取码，TTL 300s）。

### 7.3 失败排查

- 看前端向导文案（相位 + error/notice 双通道：`failed` 是红条结论，其余是黄条过程说明）。
- 看 Mac 日志：login_watch 的 stdout（launchd 日志，TODO：路径未见于仓库；手动跑即终端输出）。
- 看 Redis：`HGETALL pipeline:state:login_req`（含 phase/attempts/error）。
- 锚点失配（App 改版）→ login_watch 停手并把「屏上看到了什么」写进 error；处理方式是让 Pi 跑 `login_round`（人工兜底）重新校准 `login_map.json`，或按需重探锚点。

### 7.4 已知缺口

- **`POST /api/hr/accounts/login-request/resend`（重发验证码）目前没有消费者**：它只 `bump_resend()`（`resend_seq+1`），而 login_watch 与 login_round 都不会去点「重发」。TODO：要么补消费者，要么摘掉按钮。

## 8. 业务闭环（Pi 侧规则，权威版见 `agent/system-prompt.md` + `agent/rounds.md`）

### 8.1 main_round · outbound（打招呼，`remaining>0` 时）

`job_list_active()` → 顶部职位 tab 选定职位（按 title 对应取 job_id，不要写死 job 1）→ 该职位「推荐」tab，耗尽切「最新」→ 逐卡：读简历 → `candidate_get_or_create`（source=recommended）→ `jc_get_or_create` → `risk_check` → `screen` 评分 → 双查一人一消息 → `gated_tap(touch=True)` 点「立即沟通」（**计入每日 100**）→ 每打 3-5 个批量 dump 验证上屏 → `interaction_log(out)` → `state_transition` → `tasklog_add`。跳过已触达者（`candidate_has_out_message=true`）。`rate_limited` → 立即停打招呼、转 inbound。

### 8.2 main_round · inbound（未读消息，`remaining==0` 时）

消息页「未读」tab（只处理未读；已处理自动变已读）→ 逐会话（≤10）：读简历 7 字段 → `candidate_get_or_create`（source=inbound，**主键 `liepin_user_id` 用抓取时间戳** `ts_YYYYMMDD_HHMMSS`）→ 读详情页「沟通职位」模糊匹配 `job_list_active`（匹配不到 → `job_upsert` 录入后拿新 id）→ `risk_check` → `screen` 评分 →

- **已带附件简历**（会话有附件气泡[文件名] 或 详情页有「附件简历」区+文件名）：头像 → 资料页 → 划到底 → 附件简历 → PDF 查看器（右上角有「转发」）→ `download_attachment(...)` 下载**真实 PDF** 归档 → `interaction_log(in, attachment)` + `state_transition(receive_resume)`。**跳过触达**（对方已发简历）。
- **无附件**：判定拒绝 → `reject_hard/reject_llm`；通过 → 双查 → `gated_tap(touch=True, counts_daily=False)` 索要简历（**不计每日 100**）→ 验证上屏 → 先 `interaction_log(out)` 再 `state_transition(request_resume/await_resume)`。

### 8.3 附件与「在线简历 ≠ 附件」（防重复踩坑）

判定「有附件」的**唯一依据**是：会话里有附件气泡（文件名）或详情页有「附件简历」区+文件名。「看简历」打开的**在线简历**（带「在线」标签）不是附件。无附件时严禁 `receive_resume` / `interaction_log(msg_type='attachment')`——工具会因缺附件归档证据（`candidate.attachment_keys`，仅由 `download_attachment` 写入）**直接拒绝**；截图归档（`snapshot_put`）不写该证据。

### 8.4 审计与顺序铁律

触达 → 验证 → `interaction_log` → `state_transition` → `tasklog_add`，顺序不可颠倒（触达类事件（greet/request_resume/await_resume）在该 jc 无 out 流水时会被工具拒绝；`interaction_log` 对第二条 out 也会拒绝）。

### 8.5 72h 关闭

`awaiting_resume` 且 `resume_requested_at` **严格早于** now-72h → `no_response → closed`（恰好 72h 不关）。由 daily_round 第 1 步调 `close_stale_awaiting()` 执行；零追发（一人一消息）。

## 9. 数据模型与迁移

- 六张业务表：`accounts` / `jobs` / `candidates` / `job_candidate` / `interactions` / `review_overrides`；外加 `task_logs`（成本与事件落账）。
- `jobs.status`：`active`（参与打招呼）/ `draft`（扫到但 JD 不足 30 字或未确认读完整——**工具层不可见，不参与任何招聘动作**；在前端补 JD 后启用）。
- `candidates.liepin_user_id`：**账号内唯一**（`uq_candidate_account_liepin`），主键值为抓取时间戳。
- 迁移：`services/pipeline/alembic/versions/`，**当前 head `0012`**。schema 唯一 owner 是 Alembic（pipeline 启动即 `upgrade head`）；`alembic check` 由 `services/pipeline/tests/test_migrations.py` 守卫（模型与迁移逐列一致，含 server_default）。

## 10. 日常巡检

- **前端总览**：漏斗计数、今日触达/回传、日回收目标进度、告警条（登录态 / 风控熔断 / 今日转人工）。
- **账号与登录**：账号页看生效账号；登录向导看相位；`/api/hr/alerts` 看 `login` / `risk_paused` / `quota_used_today` / `manual_today`。
- **成本**：日报（`/api/hr/daily`）逐日 tokens/cost（**全系统口径**，不按账号拆）；`task_logs` 里 main/daily 轮次 token 汇总的 `outcome` 反映真实成败。
- **转人工队列**：`/api/hr/manual-queue`（failed_needs_manual 落账 + 派发登记反查上下文）→ ack 注记。
- **Pi 会话**：`~/.pi/agent/sessions/`（jsonl）；token 汇总靠它，排查「成本不落账」先看它。
- 常用手工检查：`redis-cli GET cua:risk:paused:{id}`、`redis-cli HGETALL pipeline:state:login_req`、`redis-cli GET msg-touch:{id}:{YYYYMMDD}`。

## 11. 常见故障表

| 现象 | 排查 / 处置 |
|---|---|
| 轮次不启动，`[skip]` 有明确原因 | 按 §5.1 对照表逐条核：窗口外 / 登录在途 / 无 active 职位 / 空退避 / 锁被占——五条各有对应文件（§5.3）与恢复路径 |
| 「没有生效账号」503 / `NoActiveAccountError` | 前端账号页尚无生效账号（或 `is_active` 全 false）：去账号页选一个（走 `switch`/`login`），不要直接改库 |
| 登录卡在 awaiting_code 超时 | 请求 TTL 30 分钟 → `expired`；验证码 TTL 300s、只能取一次；重新发起即可（`force=true` 覆盖在途请求） |
| `attempts>=3` 判 failed | login_watch 尝试 3 次未推进即停；看 error 文案（多含屏上所见），修锚点/换人工 |
| 换号失败且旧号已退 | 「手机上现在没有账号」——前端有「登回 <旧号>」入口；必须人工/系统把某个号登回去（§7.3） |
| 主线被 `.login_in_flight` 挡住 | 最长 2400s 自愈；确认轮询器在跑（`ps`/launchd），需要立即恢复可手动删该文件（确认手机不在登录页） |
| 风控熔断（`risk_paused=true`） | 人工在手机完成安全验证 → 清 `cua:risk:paused:{account_id}`（或前端操作）→ 低密度恢复；恢复前勿启动触达轮次 |
| 风控页再现（图形验证码/滑块） | **立即停止全部自动化**，绝不尝试绕过；按 §6.3 处置 |
| `.round.lock` 残留 | 锁自愈条件：PID 不存活或 mtime >3900s；都满足则下一轮自动接管。手动删前先 `ps` 确认没有 Pi/node 在跑 |
| `.inbound_state.json` 退避中但系统在干活 | 文件里看 `empty_streak`/`last_empty_ts`；30 分钟自动恢复。注意：无 active 职位的跳过**不**记退避 |
| 轮次跑完但成本没落账 / 报 `uv` 或 pi 找不到 | `run_round.py` 用 `uv run --package hr-workbuddy-pipeline` 写 `task_logs`、用探测到的 pi/node 起 Pi——launchd 的 PATH 很窄：确认 `uv` 在 PATH、pi 在 `HR_PI_CLI` 或 Homebrew 常见路径（代码已探测 `/opt/homebrew/bin/pi`、`/usr/local/bin/pi`） |
| adb 掉线（手机连不上） | adb 走 WiFi：`adb connect <手机IP>:5555`；`phone.py` 会按 `HR_ADB` → Homebrew → `~/Library/Android/sdk` → Windows 路径依次探测。掉线时所有真机动作都会 AdbError |
| screening 返回 `judge_reason="deferred: LLM unavailable"` | 检查 `.env` 的 `SCREENING_LLM_*`（key/模型名/base_url）；候选人只存快照不推进，**daily_round 的延期重判**会重新 `screen`（不再有 30min 自动巡检） |
| pipeline 起不来 / alembic 报 `Access denied for user 'hr_user'@'127.0.0.1'` | **CWD 坑**：`env_file=".env"` 按 CWD 解析。在 CT104 从 `services/pipeline` 跑 alembic/脚本时先 `export DATABASE_URL=$(grep '^DATABASE_URL=' <仓库>/.env \| cut -d= -f2-)`；同理任何读 `app.*` 的脚本要先 chdir 到仓库根 |
| 内网调用返回 404（首请求 200、其后全 404） | **代理环境变量**：`HTTP_PROXY/HTTPS_PROXY` 经 httpx `trust_env` 把 127.0.0.1 交给代理。服务侧已 `trust_env=False`；自建脚本设 `NO_PROXY=127.0.0.1,localhost,::1` |
| `ModuleNotFoundError: fastapi / pydantic_settings / mcp` | venv 被裸 `uv sync` 修剪过：`uv sync --all-packages --all-groups` |
| docker 镜像拉取失败/卡住 | 国内直连 Docker Hub 不通：走 §3.2 daocloud 加速 pull + tag |
| 端口占用（3306/6379/9000/8001/8000/3000） | `netstat -ano \| findstr :8000` 找 PID → `taskkill /PID <pid> /F`（Windows）；Mac/Linux 用 `lsof -i :8000` |
| 前端 `pnpm` 报版本错 | 走 `corepack pnpm`（根 `package.json` pin 了版本） |
| 手工改了库/表结构导致 `alembic check` 失败 | schema 唯一 owner 是 Alembic：回滚手工改动，改模型 + 写迁移，跑 `cd services/pipeline && uv run pytest tests/test_migrations.py` |

## 12. 备份与脚本

- `scripts/backup.py`：`mysqldump`（全库 → `backups/db/*.sql.gz`）+ MinIO 全量（→ `backups/minio/<ts>/`），各留最近 14 份。读 `DATABASE_URL` / `MINIO_*` 环境变量；`scripts/deploy/install.sh backup` 会装成 Mac 上的 launchd 日历任务（每日 21:30，工作窗外）——实际部署形态 TODO 待确认。
- `scripts/hr_report.py`：只读通道——候选人清单 / `--stats` 日报漏斗 / `--fetch all|<jc>` 拉快照+PDF 到 `exports/`。
- `infra/worlds/*.json`：第一代 mock 剧本，已无消费者（保留占位）。

## 13. 安全红线

- **一人一消息**：每 job_candidate 终身至多 1 条 out（打招呼/索要按平台原生动作）；发送后任何异常转人工，**不自动重发**。候选人的「终身一条消息」按候选人级（跨岗位）双查兜底。
- **触达只能走 `gated_tap(touch=True)`**；延时、每日 100、工作窗口是风控约束，不是可选项。
- **风控即停**：风控/验证页出现立即停手 + `risk_set()` + 转人工，绝不绕过。
- **数据本地**：简历 PDF 与快照只存本地 MinIO；在线简历只存 7 个最小字段快照（姓名/liepin_user_id/学历/年限/城市/薪资/经历摘要）；密钥只在 `.env`（git-ignored），任何提交文件不得含 key。
- **封号风险**：使用者知晓并接受猎聘协议封号风险，使用专门招聘子账号承担；绕过限频/窗口会显著提高风控画像。
- **真机试错禁令**：一切探查遵循 `probe_jobs.py` 的原则——人决定点哪，机器只负责看；「发职位」空白表单误提交 = 真的对外发布职位。

## 14. 待确认清单（写稿时未能从代码/仓库取证）

1. CT104 的 systemd 单元名清单（含 MySQL/Redis/MinIO）与安装形态；Mac 上三个 `com.hr-workbuddy.pi-*` plist 的内容与安装方式（仓库内无脚本）。
2. `backfill_round` 的触发方式（仓库内无定时任务；rounds.md 按轮次写，疑人工）。
3. `POST /accounts/login-request/resend` 无消费者（§7.4）。
4. `accounts.password_enc` 在「密码登录停用」之后的实际用途（代码注释称"换号失败时的回滚保障"，但当前登录路径只认验证码；`decrypt_secret` 目前只被测试引用）。
5. 文档/提示词间的数字漂移（以代码为准，待统一）：hr-tools 工具数 = 代码 32 个 `@mcp.tool()`；`agent/system-prompt.md` 写 28（未列 `login_map_get`/`login_fill_phone`/`adb_selftest`/`list_backfill_candidates` 这 4 个登录/回填工具）；`docs/architecture/` 写 26。另：`rounds.md` 写「心跳每 2 分钟」，而 launchd 任务为每 300 秒。
6. `docs/architecture/` 成文于 2026-10-14——其中的调度一节（Windows 任务计划 `hr-pi-main`/`hr-pi-daily`）与风控键写法（无账号分片）已被现状取代，建议按本文档更新。
7. `.env.example` 与 `scripts/deploy/run_service.sh` 的第一代遗留键/分支待清理（§3.6）；`scripts/deploy/uninstall.sh` 循环里还留着 scheduler/worker。
