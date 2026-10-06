"""真实账号冒烟 CLI（M1 T12）：6 步人工清单的机械执行器。

脚本运行在宿主机（真实模式需交互式桌面会话），建议在仓库根运行（.env 从
cwd 读取）。用法：

    # 默认（不带 --step）：只打印清单与用法，不执行任何动作，退出码 0
    uv run python services/cua-agent/scripts/smoke_real.py

    # ① 登录态验证（持久化 profile；需真实模式）
    $env:CUA_DRIVER_MODE = "real"
    uv run python services/cua-agent/scripts/smoke_real.py --step login

    # ③ 读在线简历（只读，不发消息）
    uv run python services/cua-agent/scripts/smoke_real.py --step read --candidate-liepin-id <测试候选人ID>

    # ④ 发送消息（唯一触达步：必须 --candidate-liepin-id + --yes 双开关）
    uv run python services/cua-agent/scripts/smoke_real.py --step send --candidate-liepin-id <ID> --yes
    #    不传 --text 时只读直查 MySQL 渲染岗位话术（{name}/{title}，与 pipeline
    #    messaging.render_message 变量契约一致）；--screenshot-png 传发送后截图
    #    则顺带跑 BrainClient verify（截图校验 + token 用量）
    uv run python services/cua-agent/scripts/smoke_real.py --step send --candidate-liepin-id <ID> --yes --screenshot-png after.png

    # ⑤ 核对延时配置 / msg-touch 令牌桶 / TaskLog 落账（只读）
    uv run python services/cua-agent/scripts/smoke_real.py --step verify

设计约束（T12 brief 逐条）：
- 默认不动作：不带 --step 只打印清单；send 必须显式 --candidate-liepin-id
  且带 --yes 确认；
- 只读复用已有组件（drivers/brain/config；verify 经 pipeline 的 DATABASE_URL
  直查 MySQL 只读），不新增任何服务逻辑；
- T8 骨架页面方法为 NotImplementedError：冒烟按「预期失败（待 T12 校准）」
  清晰报告，不崩栈；
- 任何动作前打印「将要执行什么」+ 风险提示（真实账号真实触达）；
- 发送已完成后的一切失败（verify 失败 / 大脑不可用 / 校验期意外）一律以
  「【消息已发送…】」语境报告，绝不显示「未执行」；驱动中途抛错按
  「发送结果未知」报告并提示人工确认（一人一消息，勿盲目重发）；
- 退出码：0 = 完成（含预期失败已报告）；2 = 未执行或未完成（未 --yes、
  mock 模式跑真实步、缺候选人 id、SDK/依赖未装、发送后校验失败/未通过）；
  1 = 意外错误（含发送结果未知；打印简明信息，非预期路径才含堆栈）。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[3]
CUA_DIR = REPO_ROOT / "services" / "cua-agent"
SCHEDULER_DIR = REPO_ROOT / "services" / "scheduler"
PIPELINE_DIR = REPO_ROOT / "services" / "pipeline"

DEFAULT_DATABASE_URL = "mysql+pymysql://hr_user:hr_dev_pw@127.0.0.1:3306/hr_workbuddy"
DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
TASK_LOG_LIMIT = 10  # verify 步展示最近 N 行 TaskLog
SEND_CRITERIA = "消息已成功发送并显示在会话中"  # executor._ACTIONS[SEND_MESSAGE] 同款判据
# pipeline:state:login 键（services/pipeline/app/login_state.py LOGIN_STATE_KEY）。
# 本脚本不导入 pipeline app 包（与 cua app 的包名冲突），键值直接引用。
LOGIN_STATE_KEY = "pipeline:state:login"
# MinimalResume 恰 7 字段（packages/contracts，与 E2E 脚本同款清单）
RESUME_7_FIELDS = (
    "name",
    "liepin_user_id",
    "education",
    "years_of_experience",
    "city",
    "salary",
    "experience_summary",
)


class PreconditionError(Exception):
    """前置条件不满足：消息即用户可读指引（退出码 2，不打印堆栈）。"""


# —— 模块加载（只读复用已有组件；cua app 为包导入，pipeline/scheduler 仅取 config）——


_cua_modules: dict[str, ModuleType] = {}


def _cua_module(name: str) -> ModuleType:
    """导入 services/cua-agent 的 app 包内模块（importlib 缓存）。

    依赖缺失（venv 被裸 `uv sync` 剪过）时给出可读指引，不崩栈。
    """
    if name not in _cua_modules:
        if str(CUA_DIR) not in sys.path:
            sys.path.insert(0, str(CUA_DIR))
        try:
            module = __import__(name, fromlist=["*"])
        except ImportError as e:
            raise PreconditionError(
                f"cua-agent 依赖缺失（{e}）：请在仓库根执行 `uv sync --all-packages` 后重跑"
            ) from e
        _cua_modules[name] = module
    return _cua_modules[name]


def _load_config_standalone(alias: str, path: Path) -> ModuleType:
    """经 importlib 独立加载无包内相对导入的 config 模块（pipeline/scheduler）。"""
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        raise PreconditionError(f"模块加载失败：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def _cua_settings():
    return _cua_module("app.config").get_settings()


def _scheduler_settings():
    return _load_config_standalone(
        "scheduler_config", SCHEDULER_DIR / "app" / "config.py"
    ).get_settings()


def _pipeline_settings():
    return _load_config_standalone(
        "pipeline_config", PIPELINE_DIR / "app" / "config.py"
    ).get_settings()


# —— 前置检查 ——


def _require_candidate_id(args) -> str:
    lid = (args.candidate_liepin_id or "").strip()
    if not lid:
        raise PreconditionError(
            "本步需要 --candidate-liepin-id <id>（候选人 liepin_user_id）"
        )
    return lid


def _require_real_mode(step: str):
    settings = _cua_settings()
    if settings.driver_mode != "real":
        raise PreconditionError(
            f"本步（{step}）面向真实账号冒烟，但当前 CUA_DRIVER_MODE={settings.driver_mode!r}。"
            '请以 CUA_DRIVER_MODE=real 重跑（PowerShell：$env:CUA_DRIVER_MODE = "real"）'
        )
    return settings


def _build_real_driver():
    cua_sdk = _cua_module("app.drivers.cua_sdk")
    try:
        return cua_sdk.CuaLiepinDriver()
    except cua_sdk.CuaNotInstalledError as e:
        raise PreconditionError(str(e)) from e


# —— 各步骤 ——


def _header(title: str) -> None:
    print("=" * 66)
    print(
        f"真实账号冒烟 — {title}   （{datetime.now().isoformat(sep=' ', timespec='seconds')}）"
    )
    print("=" * 66)


def _expected_not_implemented(e: NotImplementedError, note: str) -> int:
    """T8 骨架的预期失败路径：清晰提示「待 T12 校准」，不崩栈。"""
    print("【预期失败（待 T12 校准）】页面方法尚未实现：")
    print(f"    {e}")
    print(f"说明：{note}")
    print("      本提示即冒烟预期输出——T12 真实账号校准后，同一命令返回真实结果。")
    return 0


def run_login() -> int:
    _header("① 登录态验证（check_login，持久化 profile）")
    _require_real_mode("login")
    print("将要执行：CUA_DRIVER_MODE=real 构造 CuaLiepinDriver → check_login()")
    print("风险提示：只读检查登录态，不触达任何候选人；SDK 加载进程内桌面运行时。")
    print()
    driver = _build_real_driver()
    try:
        logged = driver.check_login()
    except NotImplementedError as e:
        return _expected_not_implemented(
            e,
            "T8 骨架未校准真实桌面会话（登录态判定锚点：右上角头像/「登录」按钮）。",
        )
    print(f"check_login() → {logged!r}")
    if logged:
        print("判定：已登录（持久化 profile 有效），可进入下一步。")
    else:
        print("判定：未登录 —— 请先在桌面应用扫码登录，再重跑本步。")
    return 0


def run_read(args) -> int:
    _header("③ 读在线简历（只读，不发消息）")
    lid = _require_candidate_id(args)
    _require_real_mode("read")
    print(f"将要执行：read_online_resume({lid!r}) —— 打开在线简历 + 全页截图 + 解析 7 字段")
    print("风险提示：真实账号页面操作（浏览留痕），但不会发送任何消息。")
    print()
    driver = _build_real_driver()
    try:
        screenshot, resume = driver.read_online_resume(lid)
    except NotImplementedError as e:
        return _expected_not_implemented(
            e,
            "简历卡片位置、字段锚点、无简历时页面形态待真实页面校准；本步未发送任何消息。",
        )
    print(f"截图字节数：{len(screenshot)} bytes")
    print("MinimalResume（7 字段）：")
    for field in RESUME_7_FIELDS:
        print(f"  - {field}: {getattr(resume, field)!r}")
    print("确认：本步未发送任何消息（零触达）。")
    return 0


def _render_text_from_db(lid: str) -> str:
    """只读直查 pipeline 库：取候选人最新 job_candidate 的岗位话术模板并渲染。

    话术变量契约与 pipeline messaging.render_message 一致：{name}、{title}
    （messaging.TEMPLATE_VARIABLES）。本脚本为轻量只读镜像（不导入 pipeline
    app 包——与 cua app 包名冲突）；pipeline 的 render_message 才是权威实现。
    """
    try:
        from sqlalchemy import create_engine, text as sql_text
    except ImportError as e:
        raise PreconditionError(
            f"sqlalchemy 缺失（{e}）：请 `uv sync --all-packages`，或改用 --text 显式传文本"
        ) from e
    engine = create_engine(_pipeline_settings().database_url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            row = (
                conn.execute(
                    sql_text(
                        "SELECT j.title AS title, j.template_msgs AS template_msgs, "
                        "c.name AS name "
                        "FROM candidates c "
                        "JOIN job_candidate jc ON jc.candidate_id = c.id "
                        "JOIN jobs j ON j.id = jc.job_id "
                        "WHERE c.liepin_user_id = :lid ORDER BY jc.id DESC LIMIT 1"
                    ),
                    {"lid": lid},
                )
                .mappings()
                .first()
            )
    except Exception as e:
        raise PreconditionError(
            f"读取 MySQL 失败（{e}）。数据底座是否已起"
            "（docker compose -f infra/docker-compose.yml up -d）？"
            "也可改用 --text 显式传文本跳过渲染。"
        ) from e
    finally:
        engine.dispose()
    if row is None:
        raise PreconditionError(
            f"DB 中未找到 liepin_user_id={lid!r} 的 job_candidate：无法渲染话术模板。"
            "请改用 --text 显式传文本。"
        )
    template_msgs = row["template_msgs"]
    if isinstance(template_msgs, str):
        template_msgs = json.loads(template_msgs)
    template = (template_msgs or {}).get("greet_request")
    if not template:
        raise PreconditionError(
            "岗位模板缺少 greet_request 变体（打招呼+索要合并话术）：请改用 --text 显式传文本。"
        )
    try:
        return template.format(name=row["name"], title=row["title"])
    except (KeyError, ValueError, IndexError) as e:
        raise PreconditionError(
            f"话术模板渲染失败（{e}）：可用变量 {{name}}/{{title}}；或改用 --text 显式传文本。"
        ) from e


def _verify_screenshot(screenshot: bytes):
    """经 BrainClient（OpenAIBrain）校验发送后截图；大脑不可用给可读指引。"""
    settings = _cua_settings()
    brain_mod = _cua_module("app.brain.openai_brain")
    brain = brain_mod.OpenAIBrain(
        settings.brain_base_url, settings.brain_api_key, settings.brain_model
    )
    try:
        return brain.verify_with_usage(screenshot, SEND_CRITERIA)
    except brain_mod.BrainUnavailableError as e:
        raise PreconditionError(
            f"截图校验：视觉大脑不可用（{e}）——检查 .env 的 CUA_BRAIN_*（base/key/model）"
        ) from e


def run_send(args) -> int:
    _header("④ 发送消息（唯一触达步）")
    lid = _require_candidate_id(args)
    if not args.yes:
        raise PreconditionError(
            "发送消息是真实触达动作：必须显式 --candidate-liepin-id <id> 且带 --yes 确认。"
            "（不带 --step 运行可查看完整清单与用法）"
        )
    _require_real_mode("send")
    text = args.text if args.text is not None else _render_text_from_db(lid)
    screenshot: bytes | None = None
    if args.screenshot_png is not None:
        try:
            screenshot = Path(args.screenshot_png).read_bytes()
        except OSError as e:
            raise PreconditionError(f"截图文件不可读：{e}") from e
    print("风险提示（真实账号真实触达）：将向真实候选人发送真实站内消息！")
    print("安全约束：一人一消息（每 job_candidate 终身至多 1 条 out，不可重发）——")
    print("          请确认对方是自备测试候选人。")
    print()
    print(f"将要执行：send_message(candidate_liepin_id={lid!r}, text={text!r})")
    if screenshot is not None:
        print(f"发送后校验：BrainClient verify({args.screenshot_png}, 判据=已发送上屏)")
    else:
        print("发送后校验：未提供 --screenshot-png → 跳过（真实模式自动截图源待 T12 校准）")
    print()
    driver = _build_real_driver()
    try:
        driver.send_message(lid, text)
    except NotImplementedError as e:
        return _expected_not_implemented(
            e,
            "会话输入框定位/输入/上屏确认待真实页面校准；本步未实际发送任何消息。",
        )
    except Exception as e:
        # 驱动中途抛错：消息可能已发出也可能未发出——绝不显示「未执行」
        print("【发送结果未知】send_message 抛错，消息可能已发出也可能未发出：")
        print(f"    {type(e).__name__}: {e}")
        print("    请先到会话页人工确认是否已发出，再决定是否重试（一人一消息，勿盲目重发）。")
        return 1
    # —— 从这里起消息已发出：之后一切失败都必须以「已发送」语境报告 ——
    print("【消息已发送】send_message 已返回（驱动未抛错）。")
    if screenshot is None:
        print("截图校验状态：跳过（未提供 --screenshot-png）。")
        return 0
    try:
        verdict = _verify_screenshot(screenshot)
    except PreconditionError as e:
        print(f"【消息已发送，截图校验未完成】{e}")
        print("    请人工到会话页确认消息上屏情况；勿重发（一人一消息）。")
        return 2
    except Exception as e:  # 发送已完成：校验期意外错误不打印堆栈，避免误读为发送失败
        print(
            "【消息已发送，截图校验未完成】校验过程意外错误："
            f"{type(e).__name__}: {e}"
        )
        print("    请人工到会话页确认消息上屏情况；勿重发（一人一消息）。")
        return 2
    print(f"截图校验（BrainClient verify）→ ok={verdict.ok}")
    print(f"  判据：{SEND_CRITERIA}")
    usage = verdict.usage
    print(
        f"  token 用量：prompt={usage.prompt_tokens} completion={usage.completion_tokens} "
        f"total={usage.total_tokens}"
    )
    print(
        "  注：本脚本直调大脑不落 TaskLog；经 worker 的任务才落账"
        "（核对账目见 --step verify）。"
    )
    if not verdict.ok:
        print(
            "【消息已发送，截图校验未通过】判定 ok=False：请人工到会话页确认消息"
            "是否上屏；勿重发（一人一消息）。"
        )
        return 2
    print("【消息已发送，截图校验通过】")
    return 0


def _try_worker_constants() -> ModuleType | None:
    """动作延时常量（worker.py 模块常量）。venv 缺 arq 时降级为 None。"""
    try:
        return _cua_module("app.worker")
    except PreconditionError as e:
        print(
            f"  [降级] 动作延时常量不可读（{e}）；文档值：触达 10-60s、读操作 5-15s"
        )
        return None


def _show_redis(redis_url: str) -> None:
    try:
        import redis
    except ImportError as e:
        print(
            f"  [降级] redis 包缺失（{e}）：请 `uv sync --all-packages`。"
            "文档值：键 msg-touch:{YYYYMMDDHH}，值 ≤ 20/小时"
        )
        return
    try:
        client = redis.Redis.from_url(redis_url, decode_responses=True)
        keys = sorted(client.scan_iter(match="msg-touch:*"))
    except Exception as e:
        print(f"  [降级] Redis 不可达（{e}）：请确认 compose 栈已起（{redis_url}）")
        return
    try:
        if not keys:
            print("  无 msg-touch:* 键（本小时尚无触达，或 CUA_E2E_INSTANT 直通不落桶）")
        for key in keys:
            value = client.get(key)
            ttl = client.ttl(key)
            exhausted = (
                value is not None and value.isdigit() and int(value) > 20
            )
            note = "（已触顶：值 > 20/小时）" if exhausted else ""
            print(f"  {key} = {value}  TTL={ttl}s{note}")
        state = client.get(LOGIN_STATE_KEY)
        print(
            f"  {LOGIN_STATE_KEY} = "
            f"{state if state else '（无——尚未跑过 CHECK_LOGIN）'}"
        )
    finally:
        client.close()


def _show_task_logs(database_url: str) -> None:
    try:
        from sqlalchemy import create_engine, text as sql_text
    except ImportError as e:
        print(f"  [降级] sqlalchemy 缺失（{e}）：请 `uv sync --all-packages`")
        return
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            rows = (
                conn.execute(
                    sql_text(
                        "SELECT task_id, outcome, attempt, tokens, cost, duration, "
                        "created_at FROM task_logs ORDER BY id DESC LIMIT :n"
                    ),
                    {"n": TASK_LOG_LIMIT},
                )
                .mappings()
                .all()
            )
    except Exception as e:
        print(f"  [降级] MySQL 不可达或表不存在（{e}）")
        print("   提示：请确认 compose 栈已起，且 pipeline 启动过（启动即 alembic upgrade head）")
        return
    finally:
        engine.dispose()
    if not rows:
        print("  task_logs 为空（尚无任务回调落账）——跑一次 E2E 或真实任务后重看")
        return
    print(f"  最近 {len(rows)} 行（新→旧）：")
    for row in rows:
        print(
            f"    {row['created_at']}  task={row['task_id'][:8]}…  "
            f"outcome={row['outcome']}  attempt={row['attempt']}  "
            f"tokens={row['tokens']}  cost={row['cost']}  duration={row['duration']:.1f}s"
        )


def run_verify() -> int:
    _header("⑤ 核对延时配置 / 令牌桶 / TaskLog 落账（只读）")
    print("风险提示：全部为只读查询，无任何触达。")
    print()

    print("[A] 延时与限频配置（代码实际值 + 当前 env）")
    cua = None
    try:
        cua = _cua_settings()
        print(f"  CUA_DRIVER_MODE              = {cua.driver_mode!r}（有效值 mock|real）")
        print(f"  MSG_RATE_PER_HOUR            = {cua.msg_rate_per_hour} 条/小时（触达令牌桶）")
        print(
            f"  CUA_E2E_INSTANT              = {cua.e2e_instant} "
            "（1=延时置0+桶直通，仅 E2E；真实冒烟/生产必须不设或 0）"
        )
        if cua.e2e_instant:
            print("  ⚠ 警告：CUA_E2E_INSTANT=1 会绕过限频与延时——真实冒烟前必须清除！")
        print(f"  CUA_BRAIN_MODEL              = {cua.brain_model}")
        print(
            f"  CUA_BRAIN_PRICE_PER_1K_TOKENS = {cua.brain_price_per_1k_tokens} 元"
            "（0=占位，待 T12 按视觉模型定价校准）"
        )
    except PreconditionError as e:
        print(f"  [降级] cua-agent 配置不可读：{e}")
    worker = _try_worker_constants()
    if worker is not None:
        print(
            f"  动作延时（worker 代码）       = 触达 {worker.TOUCH_DELAY_RANGE}、"
            f"读操作 {worker.READ_DELAY_RANGE}（秒，均匀随机）"
        )
        print(
            f"  令牌桶键前缀                 = {worker.BUCKET_KEY_PREFIX}"
            "（键形 {prefix}:{YYYYMMDDHH}，窗口 3600s）"
        )
    try:
        sched = _scheduler_settings()
        print(
            f"  工作窗口                     = {sched.work_window_start}–{sched.work_window_end}"
            "（[start,end)）"
        )
        print(f"  DAILY_MSG_CAP                = {sched.daily_msg_cap} 条/天（触顶停触达类派发）")
        print(
            f"  轮次间隔                     = inbound {sched.inbound_interval_seconds}s / "
            f"sweep {sched.sweep_interval_seconds}s / login_health "
            f"{sched.login_health_interval_seconds}s / reconcile "
            f"{sched.reconcile_interval_seconds}s / deferred "
            f"{sched.deferred_interval_seconds}s"
        )
    except PreconditionError as e:
        print(f"  [降级] scheduler 配置不可读：{e}")
    print()

    print("[B] Redis：msg-touch 令牌桶 + 登录态键")
    _show_redis(cua.redis_url if cua is not None else DEFAULT_REDIS_URL)
    print()

    print("[C] MySQL：task_logs 最近落账（只读直查 pipeline DATABASE_URL）")
    try:
        database_url = _pipeline_settings().database_url
    except PreconditionError as e:
        print(f"  [降级] pipeline 配置不可读（{e}），改用默认本地 DATABASE_URL")
        database_url = DEFAULT_DATABASE_URL
    _show_task_logs(database_url)
    print()
    print("核对要点：延时区间符合上表；本小时 msg-touch 值 ≤ 20；")
    print("          task_logs 行 tokens/cost 与本次冒烟 verify 用量口径一致（total_tokens）。")
    return 0


# —— 清单（默认输出）——


def print_checklist(parser: argparse.ArgumentParser) -> None:
    print("=" * 66)
    print("真实账号冒烟清单（M1 T12）—— 6 步人工核对")
    print("=" * 66)
    print(
        "安全红线（spec 决策 3/10）：仅对自备测试候选人触达；一人一消息"
        "（终身 1 条 out，不可重发）；数据全部本地不外传；"
        "猎聘账号封号风险由使用者知晓并接受。"
    )
    print()
    print("① 登录态验证（持久化 profile）——只读")
    print('   $env:CUA_DRIVER_MODE = "real"')
    print("   uv run python services/cua-agent/scripts/smoke_real.py --step login")
    print(
        "   预期：check_login() → True；若打印「待 T12 校准」即骨架未实现（预期失败），照实记录。"
    )
    print()
    print("② 登出 → 派发暂停 + 告警 + 扫码恢复（人工步骤，本脚本不驱动）")
    print(
        "   操作：桌面应用登出 → 等 scheduler login_health_round"
        "（启动即首轮，之后每小时）→ 观察 scheduler 日志出现"
        " '[login] 请扫码登录：登录态失效，已暂停全部派发' → 扫码重新登录 → 自动解除。"
    )
    print("   本步需要全栈运行（scheduler + pipeline + real worker）。结果记入 runbook 附录。")
    print()
    print("③ 读在线简历（只读，不发消息）")
    print(
        "   uv run python services/cua-agent/scripts/smoke_real.py "
        "--step read --candidate-liepin-id <测试候选人ID>"
    )
    print("   预期：打印 MinimalResume 7 字段 + 截图字节数；确认零 out 消息。")
    print()
    print("④ 发送消息（唯一触达步，双开关）")
    print(
        "   uv run python services/cua-agent/scripts/smoke_real.py "
        "--step send --candidate-liepin-id <测试候选人ID> --yes"
    )
    print("   可选：--text 显式文本；--screenshot-png 截图.png 顺带跑 BrainClient 截图校验。")
    print("   预期：合并话术文本正确（{name}/{title} 已填充）→ 发送成功 + 截图校验通过。")
    print()
    print("⑤ 核对延时配置 / 令牌桶 / TaskLog 落账（只读）")
    print("   uv run python services/cua-agent/scripts/smoke_real.py --step verify")
    print(
        "   预期：延时 10-60s（触达）/5-15s（读）；msg-touch:{YYYYMMDDHH} 计数 ≤ 20；"
        "task_logs 最近行 tokens/cost 与本次调用用量一致。"
    )
    print()
    print("⑥ 把 ①-⑤ 的输出（时间/账号/候选人/结果）记入 docs/runbook.md 附录。")
    print()
    print("本脚本不带 --step 不执行任何动作（当前即此状态，退出码 0）。")
    print()
    parser.print_help()


# —— 总装 ——


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smoke_real.py",
        description="真实账号冒烟（6 步人工清单的机械执行器；不带 --step 只打印清单）",
    )
    parser.add_argument(
        "--step",
        choices=["login", "read", "send", "verify"],
        help="执行哪一步；缺省只打印清单（不执行任何动作）",
    )
    parser.add_argument(
        "--candidate-liepin-id",
        help="候选人 liepin_user_id（read/send 必填）",
    )
    parser.add_argument(
        "--text",
        help="发送文本（send 不传则只读直查 MySQL 渲染岗位话术模板）",
    )
    parser.add_argument(
        "--screenshot-png",
        help="发送后截图文件路径（send 用：顺带跑 BrainClient 截图校验）",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="确认执行真实触达（send 必填，双开关之一）",
    )
    return parser


def dispatch(args) -> int:
    if args.step == "login":
        return run_login()
    if args.step == "read":
        return run_read(args)
    if args.step == "send":
        return run_send(args)
    if args.step == "verify":
        return run_verify()
    raise PreconditionError(f"未知 step：{args.step!r}")


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.step is None:
        print_checklist(parser)
        return 0
    try:
        return dispatch(args)
    except PreconditionError as e:
        print(f"\n[未执行] {e}")
        return 2
    except Exception:
        print("\n[意外错误]")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
