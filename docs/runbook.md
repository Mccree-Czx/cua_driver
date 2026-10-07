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
| worker 日志大脑不可用 → 任务 deferred 重判（60s） | 检查 .env `CUA_BRAIN_*`；模型必须是 `deepseek-flash`（`deepseek-v4-pro` 不支持 image 输入）。**另一已知原因（已修）**：T12 前 `response_format` 用 json_schema，DeepSeek 现拒（400 `This response_format type is unavailable now`）——已改 `json_object` + 解析容忍代码围栏 |
| 页面出现「账号行为异常」+ 图形验证码（安全验证页） | **立即停止全部自动化**（停 worker + 排空 arq 队列，防继续操作被控账号）；人工在桌面完成安全验证（绝不自动绕过）。2026-10-06 实测触发背景：连续高频真实校准（多任务连跑 + 失败任务快速重试 + 反复切页/重载，1 小时内数百次操作）。**已内置防线（2026-10-06；同日二次事件后加固）**：驱动检测风控页→立即 failed_needs_manual  不重试 + **写全局熔断标志 `cua:risk:paused`**（队列其余任务零操作直接跳过转人工，不再逐个试探）；重试默认延后 60s（`CUA_RETRY_DEFER_SECONDS`）；任务间隔默认 30s（`CUA_TASK_GAP_SECONDS`；E2E instant 自动置 0）。**恢复流程**： 人工完成安全验证并确认页面恢复正常后清除熔断标志：`redis-cli -h 127.0.0.1 DEL cua:risk:paused`；观察期内（实测同日二次触发：阈值显著降低）建议静置 ≥24h 后以超低密度恢复（任务间隔 ≥120s、单批 ≤3 个操作） |
| worker 疑似「吞队列」：队列只减、无无处理日志或处理即崩 | **环境错误 + 僵尸特性（2026-10-06 晚实测）**：任务级异常不会杀死 arq worker——配错的 worker 会循环吞掉整个队列（每个任务失败但不落 pipeline 账）。核查三项：① **cwd 必须是仓库根**（`env_file=".env"` 相对 cwd；从 `services/cua-agent` 子目录启动会缺 `CUA_BRAIN_API_KEY` 而任务级崩溃）；② `CUA_DRIVER_MODE=real`（缺省为 mock，会去找不存在的 `worlds/default.json`）；③ `PYTHONPATH=services/cua-agent`。处理：先 `pkill -9 -f "arq app.worker"` 清僵尸，修环境后把未完成任务重新入队 |
| 跑完 E2E 后真实批次的 MinIO 归档消失 | E2E `reset_state` 会清空 bucket 内 `snapshots/` 与 `resumes/` 全部对象（含生产库模式下的真实归档）。**跑 E2E 前对 MinIO 一并做备份，或选无真实归档的时段**；DB 记录（object_key）不受影响，平台侧原件仍在，必要时重跑下载任务补归档（2026-10-06 晚实测：本批 PDF×10 + 截图×10 被清） |
| worker 日志「窗口不可达（off_space_or_ax_unresolved）」 | 窗口 AX 面不可解析（风控/登录跳转或用户切屏的伴生状态）：任务已转 failed_needs_manual（保守化：不自动重试；不触发全局熔断）。确认桌面与浏览器窗口恢复后重跑该任务 |
| 任务 evidence 出现 `llm_fallback` / `fallback_exhausted` | 读取链（check_login/list_unread/read_resume/check_attachment）定位失败触发 LLM 视觉兜底：`llm_fallback`=兜底诊断与动作（用量计入 brain_tokens 账目）；`fallback_exhausted`=兜底执行后仍失败已转人工（不重试）。发送类/下载类不兜底；兜底动作全程不绕过风控检测（撞风控页同样触发全局熔断） |
| real 模式驱动方法抛 NotImplementedError | **已不适用**：T12（2026-10-06）完成 7 个页面方法校准；2026-10-07 完成第 8 个 `list_recommended`（推荐页逐卡开预览提取「简历编号」；入口/结构/出口均实测）；若再现说明代码回退 |
| 回调 `404 {"detail":"Not Found"}`；E2E 卡在 read_resume「max retries 4 exceeded」 | **本机代理环境变量**：`HTTP_PROXY`/`HTTPS_PROXY` 指向本地代理时，httpx（默认 `trust_env=True`）会把 `127.0.0.1` 的内网回调也发给代理，被拦成 404（实测特征：同一连接首请求 200、其后全 404；`http.client` 与新建连接均正常）。服务侧已用 `trust_env=False` 绕过硬编码内网调用；自建脚本请设 `NO_PROXY=127.0.0.1,localhost,::1`（或 `set HTTP_PROXY=` 清空）。排查命令：`echo $env:HTTP_PROXY` |

### 5.7 两路径流程差异（2026-10-06 策略）

- **inbound（主动咨询者）**：读在线简历 → **先探附件**（读后分流）：
  - **已有简历** → **回执 `resume_ack`**（零岗位名；派发前做候选人级一人一消息检查——已触达则跳过回执）+ **直接下载入库**（硬规则不拦收：PDF → MinIO + MySQL）→ 收到后补 LLM 评分
  - **无简历** → 硬规则 → 通过者直索要（`direct_request`，零岗位名）→ 72h 等待 → 收到 PDF 入库 → 收到后补评分（写 match_score/judge_reason 供 HR；补评分失败仅记标记不重试，M3 人工关注）
- **outbound（推荐人，M2）**：分层判定通过者打招呼+索要（`greet_request`，含岗位名），未通过判定者零触达（触达成本）。**2026-10-06 已实现**：`outbound_round`（间隔/每轮上限/爬坡参数）→ `LIST_RECOMMENDED`（推荐人页读取）→ 建档（source=recommended）→ 两层判定 → 打招呼 → 72h 等回传；**默认关闭**（`OUTBOUND_ENABLED=false`）——待 W7 真实页面校准后开启（校准清单：`docs/superpowers/plans/2026-10-06-liepin-m2.md` §7）。
- **话术零岗位名（2026-10-06 晚事故修订）**：会话「沟通职位」可能与库内岗位错位（实测：回执误写「产品经理」而候选人在聊「海外ToB渠道销售（出海品牌）」）→ inbound 双向话术（回执/直索要）不引用 {title}；outbound greet 仍带 {title}（主动触达须说明来意岗位）。

## 6. 已知限制（上线前门禁）

以下三项为历史已知缺陷，**当前状态**如下（2026-10-06 更新）：

1. **同一任务可能被重试重发**：【已修，Eb4f934】发送类任务失败后一律 `failed_needs_manual`（`post_send_failure` 证据），arq 不再同 payload 重跑；真实发送的重复窗口关闭。
2. **非 awaiting_resume 状态到达的简历附件被静默丢弃**：【已修，2026-10-06】迟到附件“只存不推进”（新状态直收入库路径 `new→resume_received` 也纳入受理范围）。
3. **「终身一条消息」按 job_candidate 粒度**：【已全域化，2026-10-06】候选人级一人一消息预检已覆盖全部 SEND 派发点（推荐人建档、screening pass、回执派发三处）+ 结果侧 jc 级兜底；跨岗位去重生效。

## 7. 安全红线

- **一人一消息（决策 3）**：每 job_candidate 终身至多 1 条 out（greet_request 合并打招呼+索要），无催促无追发。冒烟仅对自备测试候选人；`smoke_real.py --step send` 必须 `--candidate-liepin-id` + `--yes` 双开关。
- **零触达原则**：一切验证优先 mock（M1 E2E 全 mock）；真实触达前脚本打印全文与风险提示；`CUA_E2E_INSTANT=1`（延时 0 + 桶直通）**严禁**出现在真实冒烟/生产——延时、20/hr、工作窗口是风控约束不是摆设。
- **数据本地不外传**：简历 PDF 与快照仅存本地 MinIO；在线简历仅存 7 最小字段快照（姓名/liepin_user_id/学历/年限/城市/薪资/经历摘要）；真实密钥仅 `.env`（git-ignored），任何提交文件不得含 key。
- **封号风险（决策 10）**：使用者知晓并接受猎聘协议封号风险，使用专门招聘子账号承担；任何绕过限频/延时/窗口的操作都会显著提高风控画像。

## 8. 常驻部署与备份（上线硬化，2026-10-06）

- **常驻守护（launchd）**：`scripts/deploy/install.sh` 安装四服务（pipeline/screening/scheduler/worker），
  `KeepAlive` 崩溃自动拉起、`RunAtLoad` 登录即起；环境由 `run_service.sh` 固化
  （cwd=仓库根、NO_PROXY、CUA_DRIVER_MODE=real、PYTHONPATH——僵尸 worker 教训的对策）；
  日志在 `.run/logs/`；卸载：`scripts/deploy/uninstall.sh`。
  前置：Docker（redis/minio）+ MySQL 已运行。worker 依赖交互式桌面会话——锁屏期间任务转人工，恢复后自动继续。
- **备份**：`scripts/backup.py`（mysqldump → `backups/db/*.sql.gz` + MinIO 全量 → `backups/minio/<ts>/`，
  各保留最近 14 份）。建议上线后每日 21:30（工作窗外）跑：launchd 或 crontab 均可。
- **E2E 数据面隔离**：E2E 跑在测试库 `hr_workbuddy_test` + bucket `hr-workbuddy-e2e` + Redis `/1`；
  指向生产库会直接拒跑（`E2E_ALLOW_PROD=1` 可强行绕过，勿在生产批次期间使用）。
- **HR 查看通道（M3 前凑合）**：`scripts/hr_report.py`——候选人清单（默认）/ `--stats` 每日漏斗与
  索要→回传转化率 / `--fetch all|<jc>` 拉取快照+PDF 到 `exports/`（只读脚本）。

## 附录：冒烟结果记录

> 每次真实账号冒烟按日期追加一行；①-⑤ 逐行记录输出结论（含「待 T12 校准」的预期失败）。

| 日期 | 执行人 | 步骤 | 命令/操作 | 结果 | 备注 |
|---|---|---|---|---|---|
| 2026-10-06 | Qoder(T12) | ① 登录态 | `smoke_real.py --step login` | **通过**：`check_login() → True`（首次扫描 2.1s / 缓存 0.2s） | macOS 迁移后重校：窗口判据=地址栏含 liepin.com；登录锚点=后台导航 |
| 2026-10-06 | Qoder(T12) | ② 登出/恢复 | （未执行） | 待办 | 需全栈运行 + 人工扫码配合（本地登出→告警→扫码恢复） |
| 2026-10-06 | Qoder(T12) | ③ 读简历 | `--step read --candidate-liepin-id e37fdde092f5Yc6f103cf422b` | **通过**：截图 1,062,579B + 7 字段全对（梁女士/硕士/工作2年/佛山/11-22k×12薪/摘要全文） | 零触达；liepin_user_id 真实来源=批量预览页「简历编号」 |
| 2026-10-06 | Qoder(T12) | ④ 发消息 | `--step send --candidate-liepin-id e37fdde092f5Yc6f103cf422b --yes --text "您好 梁女士，⋯发一份简历吗？"` | **通过**：消息上屏（16:02，截图目视确认）；输入读回校验+发送后上屏校验全过 | 唯一真实触达；梁女士的一人一消息额度已消耗，不可对其重发 |
| 2026-10-06 | Qoder(T12) | ⑤ 账目核对 | `--step verify` | **通过（配置面）**：real / 20条·小时 / `CUA_E2E_INSTANT` 未设 / 延时区间正确 / 工作窗口 08:00–20:00 | 本小时无 `msg-touch:*` 键（直连脚本不经 worker 延时/桶）；worker 级延时·桶·TaskLog 待全栈真实链路验证 |
| 2026-10-06 | Qoder(T12) | 联调切片（附加） | 手工入队 CHECK_LOGIN（模拟 scheduler）→ pipeline + real worker | **通过（全链）**：worker `outcome=success`（19.7s=延时~12s+执行）；`/internal/state/login` → `is_login=true`；`task_logs` 落账 tokens=1176 / duration=7.78s | 期间发现并修复：DeepSeek 拒 json_schema（400）→ 大脑改 `json_object`（单测 57 绿）；附件链同步实测：check_attachment=True、download_attachment 得 322,966B PDF |
| 2026-10-06 | Qoder(T12) | inbound 全链（附加） | 手工入队 LIST_UNREAD →  真实 worker 链（screening 用拒绝桩保零触达） | **部分完成后遇风控停机**：LIST_UNREAD 成功（建档 8 位真实候选人）；2 个 READ_RESUME 成功（其余遇风控） ；**触发猎聘风控（账号行为异常验证码）→ 立即停机** | 零触达保持（interactions 未增）；处置与恢复建议见 `.scratch/liepin_calib/risk_control_incident.md`；恢复前需人工完成安全验证 + 控制操作密度 |
| 2026-10-06 | Qoder(T12) | 策略回溯批次（check-first 分流） | 重开 10 人（终态→new）→ 逐个入队 CHECK_ATTACHMENT → 真实 worker（120s 间隔）级联：有简历→回执+下载入库 / 无→硬规则→直索要 | **通过（10/10）**：全部 resume_received（补评分 15-55 落账）；共 9 条回执真实发出；梁女士仅入库不回执（候选人级一人一消息拦截生效）；零风控、队列/熔断键终态清零 | 硬规则已剔除年限（保留 min_education=本科）。事故与修复：①僵尸 worker（mock 缺 world + 任务级崩溃不退出，循环吞 10 个任务）→ pkill 后带正确环境重入队；②**话术错位事故**：已发 5 条回执含「产品经理」岗位名（会话实际「海外ToB渠道销售（出海品牌）」，库内岗位为 M1 演示岗）→ 修订为**话术零岗位名**并撤销未发 4 条重入队；已发 5 条按一人一消息不可撤回；③E2E 验证后 MinIO 真实归档被清（PDF×10+截图×10，DB 记录完整，需要时补下载） |

| 2026-10-07 | Qoder(T12) | 第三次风控（终判：深夜波次 + 累计过载） | 检出：08:05 校准任务侧只读探针（窗口不可达自停）+ 用户晨检发现验证页；驱动实测抛 `RiskControlDetectedError` | **账号第三次进入安全验证**；当夜（10-06 23:25→00:24）曾以 120s 间隔连跑探针+补归档 20 任务（含批量页连续 10 次下载）| **纪律升级（第四次起执行）**：①深夜操作永久禁止（仅 [08:00,20:00) 窗口内）②单批 ≤3 操作、≥120s 间隔 ③连续两次风控后自动强制静置 ≥24h，第三次后 ≥48h ④风控后恢复首日仅只读、单任务人工目视。证据：TCC 授权记录、`desktop_fail.png`、0 条任务落账（校准运行未产生账号操作） |
- **HR 工作台（M3 最小可用，2026-10-07）**：`apps/hr-console`（Next.js 16 + Tailwind 4）。
  启动：`cd apps/hr-console && pnpm install && pnpm dev`（:3000）或 `pnpm build && pnpm start`；
  `/api/*` 由 next.config rewrites 代理至 pipeline:8000；入口=本机 localhost（spec §7-6）。
  页面：总览（漏斗/日报/阈值调参）、候选人（两级详情抽屉/复核推翻/手动重跑）、人工队列（ack/重跑）。
  注意：本机 pnpm 由 corepack pin 为 11.7.0（根 `package.json` packageManager；
  pnpm 12.4.2 缺 `pnpm.cjs` 会导致 shim 崩溃）。

## 第四次账号行为验证（2026-10-07 13:31）——已处置

- **现象**：批量预览简历页（**昨日 batch token 链接**）被 read 回退路径第三次访问后，
  跳转「账号行为异常」安全验证页（图形验证码，需人工完成）
- **当日账号动作盘点**：12:2x 三点只读（验证恢复后）；13:06「立即沟通」实测 1 点
  （真实触达 艾先生）；13:18「向TA索要」实测 1 点（真实触达 唐女士）；
  13:2x-13:33 list/read 校准（多次预览开合 + 批量页重访）
- **根因**：① 批量页（showbatchresumelist）属敏感页，旧 batch token 反复访问触发行为判定；
  ② 推荐人 read 走"批量页优先"属设计错误（推荐人不在批量页，白白踩敏感页）
- **修复（代码层，已提交）**：推荐人 read 直连推荐页（`prefer_recommend`，由
  `context.source` 驱动）；`_goto_recommend` 覆盖层自愈（列表锚点缺席→整页重载）；
  多标签窗口按标签栏标题聚焦猎聘标签（不再依赖 Cmd+1）；`request_resume` 收尾整页重载
- **处置纪律**：验证完成前零操作；完成后**静置 ≥48h**（第四次强化口径）；期间仅本地工程
- **待人工**：请在浏览器完成图形验证码
- **真实触达记录（不可撤回）**：艾先生 13:06（默认招呼）/ 唐女士 13:18（招呼+索要请求）

## 恢复窗口 SOP（第四次事件后，≥48h 静置结束起）

前提：静置期满（最早 10-09 13:31 后）、工作窗口 [08:00,20:00) 内、风险标志为空。
逐步执行，每操作点 ≥180s；任一点异常（验证页再现/元素异常）→ 立即停机、延期下一窗口。

- Step 1（1 点只读）：打开推荐页，确认无「账号行为异常」页 → 截图存证
- Step 2（1 点只读）：read 新路径复验——`prefer_recommend=True` 直连推荐页读 1 人
  （7 字段 + 截图）；不得再访问批量页
- Step 3（≤2 发送）：受控首批——内部 API 入队 LIST_RECOMMENDED(limit=2) → 真实 worker
  全链（read → 两层判定 → 向TA索要 → awaiting）；发送前后截图；发送名单与系统文案
  执行前列出
- Step 4：汇报 + 定 OUTBOUND 观察期节奏（轮次/限额/是否开自动）
- 铁律：批量页（showbatchresumelist）为敏感页——除 inbound 正常单次流程外不得回访；
  旧 batch token 链接禁止再次打开

## 常驻部署现状（2026-10-07 分批安装）

- **已常驻（launchd 直连解释器）**：`com.hr-workbuddy.{pipeline,screening,worker}` +
  `com.hr-workbuddy.backup`（每日 21:30 日历任务，DB+MinIO，各留 14 份）
- **scheduler 暂缓**：它会启动后 5min 内自动跑 `inbound_round`（账号操作）——
  仅在账号恢复窗口进入阶段 2（观察期）后执行 `bash scripts/deploy/install.sh scheduler`
- **console**：仍手工 `cd apps/hr-console && pnpm start`（:3000）
- 重装/补装：`bash scripts/deploy/install.sh [服务名...]`（默认 pipeline screening worker backup）
- 查看：`launchctl list | grep hr-workbuddy`；日志：`.run/logs/<名>.{out,err}.log`
- **跑 E2E 前必须先让出 8000**：
  `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.hr-workbuddy.pipeline.plist`
  跑完后 `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hr-workbuddy.pipeline.plist`
- 手动立跑一次备份：`DATABASE_URL=... MINIO_BUCKET=hr-workbuddy .venv/bin/python scripts/backup.py`
