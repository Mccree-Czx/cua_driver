# hr_workbuddy 运维手册（runbook）

> M1 交付配套（T12）。约定：标注「待 T12 真实校准」的行为，以真实账号冒烟（§4）的实际结果为准；冒烟结果记入文末附录。

## 1. 架构速览

```
                ┌────────────────────────────────┐
                │ scheduler（APScheduler，宿主机） │
                │ 工作窗口 / 5 轮次 / 登录健康暂停   │
                │ / 配额对账 / 在途去重            │
                └──────┬────────────────┬────────┘
        入队 AtomicTask │                │ HTTP：list_jobs / quota/today
                        ▼                │      / state/login / sweeps
┌───────────────────┐  arq 队列  ┌────────────────────────────┐
│ Redis             │◄──────────►│ pipeline（FastAPI :8000）   │──► MySQL 8.4（唯一写入口：
│ · arq 任务队列      │            │ 状态机 / 编排 / 话术渲染      │     jobs / candidates /
│ · 派发登记表(TTL72h)│            │ / 一人一消息 / TaskLog 落账   │     job_candidate /
│ · 在途索引(TTL 1h)  │            └──────▲────────────┬────────┘     interactions / task_logs）
│ · 令牌桶 msg-touch:*│                   │回调 result  │ 归档
│ · 登录态键          │                   │/artifact   ▼
└───────────────────┘           ┌───────┴───────────┴─────────┐
        ▲ 触达前权威桶检查        │ cua-agent arq worker          │   MinIO
        └───────────────────────┤（宿主机，real 需桌面会话）      │（snapshots/ 快照、
                                │ CUA SDK 驱动 + 视觉大脑        │  resumes/ PDF，预签名
                                └──────────────────────────────┘   ≤15min）
                                                   ▲ 评分旁路：pipeline 同步调用
                                  ┌────────────────┴──────────┐
                                  │ screening（FastAPI :8001） │
                                  │ 硬规则 + LLM 评分 + 降级     │
                                  └───────────────────────────┘
```

要点：

- **派发链**：scheduler 与 pipeline 都把 `AtomicTask` JSON 入 arq（Redis）；cua-agent worker 在宿主机执行页面动作，完成后 HTTP 回调 pipeline（`POST /internal/tasks/{id}/result`、`/artifact`）；pipeline 是 MySQL/MinIO 的唯一写入口，收到结果后推进状态机并入队后继任务。
- **screening 是旁路**：pipeline 在评分点同步调用 `POST /screen`，不进 arq；LLM 不可用走 degraded 路径（只存快照不推进，deferred 巡检 30min 自动重判）。
- **Redis 四用途**：arq 队列、派发登记表（`pipeline:dispatched:{task_id}`，TTL 72h）、在途索引（`pipeline:dispatched:idx:{type}:{jc_id}`，TTL 1h）、触达令牌桶（`msg-touch:{YYYYMMDDHH}`，20/hr）+ 登录态键（`pipeline:state:login`）。
- 状态机 11 态（new → screened_pass/rejected_* → … → closed），详见 spec v1.6 §4。

## 2. 环境准备

### 2.1 Docker 数据底座

```powershell
# 起栈（已在跑则复用；本仓库用 `up -d` + 探活，见下方注记）
docker compose -f infra/docker-compose.yml up -d

# 冒烟验证 MySQL / MinIO / Redis
uv run pytest infra/tests -m infra

# 停栈；连数据卷一起清（开发重置用，慎）
docker compose -f infra/docker-compose.yml down
docker compose -f infra/docker-compose.yml down -v
```

注记（实测经验）：

- 首次 `up -d --wait` 可能因 mysql 初始化自带 restart 周期超时退出 1——等约 30s 重跑即全 healthy（healthcheck 定义本身正确）。
- `minio-init` 是一次性容器（建 `hr-workbuddy` 桶后退出 0），部分 compose 版本 `--wait` 会把它判为失败——用 `up -d` + `infra` 冒烟测试探活最稳（E2E 脚本同款手法）。
- **镜像加速**（Docker Hub 直连不通时，daocloud 系可用；拉完 tag 成 compose 引用名即可，引用名不变）：

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

- docker.io 的 `minio/minio`、`minio/mc` 自 2022 年 10 月停更（MinIO 官方迁往 quay.io），compose 已直接引用 `quay.io/minio/minio` + `quay.io/minio/mc`（镜像加速时 tag 成这两个引用名即可）。

### 2.2 Python 环境（uv workspace）

```powershell
uv sync --all-packages    # 必须 --all-packages！
```

- 裸 `uv sync` 会把 `.venv` 修剪到仅根 dev 组（丢 arq/apscheduler/fastapi 等成员依赖）；`uv run` 不触发该修剪。
- 拉包慢时可用一次性镜像源 `$env:UV_DEFAULT_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"`；uv.lock 已归一为 pypi.org，勿把镜像写进 lock。

### 2.3 .env 配置清单（仓库根 `.env`，git-ignored）

从 `.env.example` 复制后填入真实值。代码实际读取的变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `mysql+pymysql://hr_user:hr_dev_pw@127.0.0.1:3306/hr_workbuddy` | pipeline 读写库（与 compose 默认一致） |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | 队列 / 注册表 / 令牌桶 / 登录态 |
| `MINIO_ENDPOINT` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | `127.0.0.1:9000` / `minioadmin` / `minioadmin` | 对象存储 |
| `SCREENING_URL` | `http://127.0.0.1:8001` | pipeline 调 screening 的地址 |
| `PIPELINE_URL` | `http://127.0.0.1:8000` | scheduler / cua-agent 回调地址 |
| `CUA_DRIVER_MODE` | `mock` | **有效值仅 `mock`\|`real`**（`.env.example` 旧值 `local` 会触发 pydantic 校验错误，已修正为 `mock`） |
| `CUA_BRAIN_BASE_URL` | `https://api.deepseek.com` | 视觉大脑 OpenAI 兼容端点 |
| `CUA_BRAIN_API_KEY` | 空 | **DeepSeek key 只放本文件，绝不入库** |
| `CUA_BRAIN_MODEL` | `deepseek-flash` | 实测支持 image 输入；`deepseek-v4-pro` 是纯文本模型，不可用作视觉大脑 |
| `CUA_BRAIN_PRICE_PER_1K_TOKENS` | `0` | 成本账目单价占位 0——**待 T12 按视觉模型定价校准** |
| `CUA_WORLD_PATH` | `worlds/default.json` | mock 模式 World 剧本路径 |
| `CUA_E2E_INSTANT` | 不设 | `1` = 延时置 0 + 令牌桶直通，**仅 M1 E2E 用；真实冒烟/生产绝不设** |
| `MSG_RATE_PER_HOUR` | `20` | 触达限频（spec §3） |
| `WORK_WINDOW_START` / `WORK_WINDOW_END` | `08:00` / `20:00` | 工作窗口 `[start, end)` |
| `DAILY_MSG_CAP` | `240` | 日触达上限，触顶停触达类派发 |
| `INBOUND_INTERVAL_SECONDS` 等 5 项 | 300 / 600 / 3600 / 3600 / 1800 | inbound / sweep / login_health / reconcile / deferred 轮次间隔 |
| `SCREENING_LLM_BASE_URL` / `_API_KEY` / `_MODEL` | `https://api.deepseek.com` / 空 / `deepseek-flash` | 简历评分文本模型 |

`.env.example` 中的 `LLM_DEFAULT_THRESHOLD` / `RESUME_TIMEOUT_HOURS` 当前**无代码消费**（阈值 70 与 72h 为代码内定值），保留占位。

## 3. 服务启动（顺序）

1. 数据底座（§2.1 compose 栈）。
2. **pipeline**（:8000，启动即 `alembic upgrade head`，幂等——schema 唯一 owner）：

```powershell
uv run uvicorn app.main:app --app-dir services/pipeline --host 127.0.0.1 --port 8000
```

3. **screening**（:8001）：

```powershell
uv run uvicorn app.main:app --app-dir services/screening --host 127.0.0.1 --port 8001
```

4. **scheduler**（APScheduler，阻塞进程）：

```powershell
cd services/scheduler; uv run python -m app.main
```

5. **cua-agent arq worker**（宿主机；mock 无桌面依赖，**real 需交互式桌面会话**）：

```powershell
cd services/cua-agent
$env:CUA_DRIVER_MODE = "mock"   # 或 "real"
$env:PYTHONPATH = "$PWD"        # arq CLI 导入 app.worker 与 cwd 无关，必须显式给
uv run arq app.worker.WorkerSettings
```

**mock / real 切换**：

| 开关 | mock（默认） | real |
|---|---|---|
| `CUA_DRIVER_MODE` | `mock`：FakeLiepinDriver + MockBrain，行为由 `CUA_WORLD_PATH` 剧本决定，零外部 I/O | `real`：CuaLiepinDriver（PyPI `cua-driver` 0.30.4，SDK 自带进程内 runtime，不依赖已装桌面 exe）+ OpenAIBrain（.env 真实 key） |
| 桌面会话 | 不需要 | 需要（真实猎聘企业版后台已登录） |
| `CUA_E2E_INSTANT` | E2E 置 1（延时 0 + 桶直通） | **禁止置 1** |

## 4. 真实账号冒烟（6 步）

执行器：`services/cua-agent/scripts/smoke_real.py`（宿主机运行，仓库根执行；不带 `--step` 只打印清单退出 0，不执行任何动作；`send` 必须 `--candidate-liepin-id <id>` + `--yes` 双开关）。

前置：数据底座已起；`.env` 已填 DeepSeek key；`CUA_DRIVER_MODE=real`；桌面会话已登录猎聘企业版后台；已确认**自备测试候选人**的 liepin_user_id。

| 步 | 操作 | 命令 / 预期 |
|---|---|---|
| ① 登录态 | 只跑 check_login 验证持久化 profile 登录态 | `$env:CUA_DRIVER_MODE = "real"; uv run python services/cua-agent/scripts/smoke_real.py --step login` → 预期 `check_login() → True`；打印「待 T12 校准」= 骨架未实现（预期失败），照实记录 |
| ② 登出恢复 | 验证派发暂停 + 告警 + 扫码恢复（**人工步骤**，脚本不驱动；需全栈运行：scheduler + pipeline + real worker） | 桌面应用登出 → scheduler 日志出现 `[login] 请扫码登录：登录态失效，已暂停全部派发`（启动即首轮，之后每小时一轮）→ 派发暂停 → 扫码重新登录 → 下一轮自动解除。期间可用 `--step verify` 观察 `pipeline:state:login` 键变化 |
| ③ 读简历 | 对自备测试候选人 read_resume（截图 + 7 字段，**不发消息**） | `uv run python services/cua-agent/scripts/smoke_real.py --step read --candidate-liepin-id <测试候选人ID>` → 打印 MinimalResume 7 字段 + 截图字节数；确认零 out 消息 |
| ④ 发消息 | **仅对测试候选人** send_message（截图校验 + 合并话术文本正确） | `uv run python services/cua-agent/scripts/smoke_real.py --step send --candidate-liepin-id <测试候选人ID> --yes [--text 文本] [--screenshot-png after.png]` → 话术 `{name}/{title}` 已填充、发送成功、`--screenshot-png` 提供时 BrainClient verify 通过；不传 `--text` 时只读直查 MySQL 渲染岗位话术 |
| ⑤ 核对账目 | 延时 / 令牌桶 / TaskLog token 落账（只读） | `uv run python services/cua-agent/scripts/smoke_real.py --step verify` → 延时 10-60s（触达）/5-15s（读）、`msg-touch:{YYYYMMDDHH}` ≤ 20、`task_logs` 最近行 tokens/cost 与 verify 用量一致、`CUA_E2E_INSTANT` 未设 |
| ⑥ 记录 | 把 ①-⑤ 输出（时间/账号/候选人/结果）记入本文末附录 | 手工粘贴 |

注意：①②③⑤ 均零触达；只有 ④ 真实触达且带双开关。**每个候选人一生只允许 1 条 out 消息（决策 3），冒烟候选人不可复用重发。**

## 5. 日常运维

### 5.1 工作窗口与轮次

- 工作窗口默认 `08:00–20:00`（`[start, end)`，20:00 整点起停止派发）；窗口外调度照常触发但零派发。
- 五轮次：inbound 5min、awaiting_resume_sweep 10min、login_health 1h、daily_quota_reconcile 1h、deferred_sweep 30min；服务启动即各跑首轮（不延迟一个 interval）。
- 轮次间隔、窗口、日上限均可在 .env 覆盖。

### 5.2 配额对账与限频

- **20 条/小时**：Redis Lua 令牌桶 `msg-touch:{YYYYMMDDHH}`；worker 在触达前做权威检查，桶耗尽即把任务 `_defer_by` 重排到下一小时窗口（不失败、不烧重试预算）。
- **240 条/天**：scheduler 每小时对账当日 out 计数；触顶 → 告警日志 + 停止触达类派发（deferred_sweep 与 awaiting_resume_sweep 的 CHECK_ATTACHMENT 入队均跳过）。已知披露：inbound 链（LIST_UNREAD→READ_RESUME→SEND_MESSAGE）由 pipeline 编排入队，在 scheduler 配额闸半径外——M2 动态阈值/配额联调时补。
- 回落（当日计数 < 上限）自动恢复派发。

### 5.3 72h 关闭语义

- `awaiting_resume` 且 `resume_requested_at` 严格早于 now-72h → `no_response → closed`（恰好 72h 不关）。
- 巡检由 awaiting_resume_sweep 每 10min 委托 pipeline（`POST /internal/sweeps/stale-awaiting`）。
- **不催促、不追发**（决策 3）：72h 无附件直接关闭，out 消息始终恰 1 条。

### 5.4 登录健康

- login_health_round 每轮入队 CHECK_LOGIN 并查最近结果（Redis `pipeline:state:login`）；失效 → 暂停全部派发 + 告警「请扫码登录」；扫码恢复后自动解除。暂停期间仍持续入队 CHECK_LOGIN（探测恢复路径）。

### 5.5 E2E 重跑（mock，一条命令）

```powershell
uv run python scripts/run_m1_e2e.py          # 一条命令跑全部（剧本 A + 剧本 B）
uv run pytest tests/e2e -m e2e -v            # pytest 包装（同一套逻辑）
```

注意：E2E 会 TRUNCATE pipeline 各表、`FLUSHDB` Redis、删除 MinIO snapshots/resumes 前缀对象——**仅在开发库运行**。

### 5.6 常见故障表

| 现象 | 排查 / 处置 |
|---|---|
| docker 镜像拉取失败/卡住 | 国内直连 Docker Hub 不通：走 §2.1 daocloud 加速 pull + tag |
| 首次 `up -d --wait` 超时退出 1 | mysql 首次初始化自带 restart 周期：等约 30s 重跑；或改用 `up -d` + infra 冒烟探活 |
| `--wait` 报 minio-init exited(0) | 一次性容器属预期：用 `up -d` + `uv run pytest infra/tests -m infra` 探活 |
| 端口占用（3306/6379/9000/9001/8000/8001） | `netstat -ano \| findstr :8000` 找 PID → `taskkill /PID <pid> /F` |
| `ModuleNotFoundError: arq / apscheduler / pydantic_settings` | venv 被裸 `uv sync` 修剪过：`uv sync --all-packages` |
| worker 起不来 / arq 队列无人消费 | 检查 `PYTHONPATH`（arq CLI 导入 app.worker 需要，见 §3）；Redis 可达；`CUA_DRIVER_MODE` 取值合法（mock\|real，勿写 local） |
| scheduler 日志 `[login] 请扫码登录…` 且派发暂停 | 登录态失效：桌面应用扫码重新登录，下一轮自动解除；状态键 `pipeline:state:login` |
| screening 返回 `judge_reason="deferred: LLM unavailable"`（降级） | 检查 .env `SCREENING_LLM_*`（key/模型名）；候选人不推进，deferred_sweep 30min 自动重判 |
| worker 日志大脑不可用 → 任务 deferred 重判（60s） | 检查 .env `CUA_BRAIN_*`；模型必须是 `deepseek-flash`（`deepseek-v4-pro` 不支持 image 输入） |
| real 模式驱动方法抛 NotImplementedError | **预期**（T8 骨架未校准，待 T12）：冒烟脚本会清晰提示「待 T12 校准」，不是故障 |
| 回调 `404 {"detail":"Not Found"}`；E2E 卡在 read_resume「max retries 4 exceeded」 | **本机代理环境变量**：`HTTP_PROXY`/`HTTPS_PROXY` 指向本地代理时，httpx（默认 `trust_env=True`）会把 `127.0.0.1` 的内网回调也发给代理，被拦成 404（实测特征：同一连接首请求 200、其后全 404；`http.client` 与新建连接均正常）。服务侧已用 `trust_env=False` 绕过硬编码内网调用；自建脚本请设 `NO_PROXY=127.0.0.1,localhost,::1`（或 `set HTTP_PROXY=` 清空）。排查命令：`echo $env:HTTP_PROXY` |

## 6. 已知限制（M2/T12 前置门禁）

以下三项为 M1 范围内的已知缺陷，**真实模式操作前（T12 真实模式校准、M2 真实发送）必须先修**：

1. **同一任务可能被重试重发**：worker 在真实模式下 verify 失败 / 大脑不可用会把任务重排重试并重发同一条消息——pipeline 的一人一消息闸只防第二条**任务**的落库，防不住同一任务的重复发送。建议发送类任务失败一律置 `failed_needs_manual`（人工介入），T12 真实模式校准与 M2 真实发送前必须落实。
2. **非 awaiting_resume 状态到达的简历附件被静默丢弃**：72h 关闭与在途下载链之间存在竞态窗口（候选人已被 72h 巡检关闭、附件才到达）——真实模式操作前必须改为「只存不推进」（附件照常落库，状态推进交由人工/复核流程）。
3. **「终身一条消息」按 job_candidate 粒度**：同一候选人对多个岗位会被各发一条（决策 3 的约束半径是单个 job_candidate）——M2 计划需补跨岗位合并或全局 out 去重。

## 7. 安全红线

- **一人一消息（决策 3）**：每 job_candidate 终身至多 1 条 out（greet_request 合并打招呼+索要），无催促无追发。冒烟仅对自备测试候选人；`smoke_real.py --step send` 必须 `--candidate-liepin-id` + `--yes` 双开关。
- **零触达原则**：一切验证优先 mock（M1 E2E 全 mock）；真实触达前脚本打印全文与风险提示；`CUA_E2E_INSTANT=1`（延时 0 + 桶直通）**严禁**出现在真实冒烟/生产——延时、20/hr、工作窗口是风控约束不是摆设。
- **数据本地不外传**：简历 PDF 与快照仅存本地 MinIO；在线简历仅存 7 最小字段快照（姓名/liepin_user_id/学历/年限/城市/薪资/经历摘要）；真实密钥仅 `.env`（git-ignored），任何提交文件不得含 key。
- **封号风险（决策 10）**：使用者知晓并接受猎聘协议封号风险，使用专门招聘子账号承担；任何绕过限频/延时/窗口的操作都会显著提高风控画像。

## 附录：冒烟结果记录

> 每次真实账号冒烟按日期追加一行；①-⑤ 逐行记录输出结论（含「待 T12 校准」的预期失败）。

| 日期 | 执行人 | 步骤 | 命令/操作 | 结果 | 备注 |
|---|---|---|---|---|---|
| | | ① 登录态 | | | |
| | | ② 登出/恢复 | | | |
| | | ③ 读简历 | | | |
| | | ④ 发消息 | | | |
| | | ⑤ 账目核对 | | | |
