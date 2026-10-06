"""读取链 LLM 视觉兜底（2026-10-06 二次风控事件后新增）。

适用范围（硬约束，由 executor 白名单 + 本模块共同保证）：
- 仅读取链任务（check_login / list_unread / read_resume / check_attachment）的
  LocatorFailedError（页面/元素定位失败）；
- 发送类（send_message）与下载类（download_attachment）不兜底；
- 风控页、窗口不可达原样上抛（防线优先——绝不由兜底"处理"风控/环境问题）。

动作优先级：click_text（AX 语义定位）→ 失败才允许 click_coords（截图像素÷比例）。
置信度低于阈值不执行动作（保守转人工）。单次 recover 至多执行一个动作；是否
重试原动作与兜底次数上限由调用方（executor）控制。
"""

from dataclasses import dataclass, field

from hr_workbuddy import AtomicTask, FallbackSuggestion

from app.brain.usage import BrainUsage
from app.drivers.cua_sdk import RiskControlDetectedError, WindowUnavailableError

FALLBACK_MIN_CONFIDENCE = 0.6


@dataclass(frozen=True)
class FallbackOutcome:
    """兜底结果：诊断 + 动作执行情况 + suggest 用量（供 cost 记账）。"""

    diagnosis: str
    action: str
    target: str
    confidence: float
    executed: bool
    usage: BrainUsage = field(default_factory=BrainUsage)

    def meta(self) -> dict:
        """evidence.llm_fallback 字段值（可观测/复盘）。"""
        return {
            "diagnosis": self.diagnosis,
            "action": self.action,
            "target": self.target,
            "confidence": self.confidence,
            "executed": self.executed,
        }


class LlmFallback:
    """截图 → brain.suggest → 执行修复动作。driver/brain 均为注入（测试可假）。"""

    def __init__(
        self, driver, brain, *, min_confidence: float = FALLBACK_MIN_CONFIDENCE
    ) -> None:
        self._driver = driver
        self._brain = brain
        self._min_confidence = min_confidence

    def recover(self, task: AtomicTask, error: Exception) -> FallbackOutcome | None:
        """尝试修复页面状态：返回诊断结果；不适合/失败返回 None（原错误照常处理）。

        风控/窗口异常一律上抛（绝不吞掉）：截图与所有动作均经驱动，驱动内建风控
        检测——兜底流程撞上风控页/窗口不可达时与原操作同一防线处理。
        """
        try:
            screenshot = self._driver.capture_desktop_png()
        except (RiskControlDetectedError, WindowUnavailableError):
            raise
        except Exception:
            return None  # 截图失败：放弃兜底（原错误照常处理）
        context = f"任务 {task.type.value} 失败：{error}"
        try:
            suggestion, usage = self._suggest(screenshot, context)
        except Exception:
            return None  # 诊断失败：放弃兜底（不重试）
        if suggestion.confidence < self._min_confidence or suggestion.action == "none":
            return FallbackOutcome(
                suggestion.diagnosis,
                suggestion.action,
                suggestion.target,
                suggestion.confidence,
                executed=False,
                usage=usage,
            )
        executed = self._execute(suggestion)
        return FallbackOutcome(
            suggestion.diagnosis,
            suggestion.action,
            suggestion.target,
            suggestion.confidence,
            executed=executed,
            usage=usage,
        )

    def _suggest(self, screenshot: bytes, context: str) -> tuple[FallbackSuggestion, BrainUsage]:
        """诊断（优先带用量扩展；仅实现协议 verify 的大脑按零用量记账）。"""
        suggest_with_usage = getattr(self._brain, "suggest_with_usage", None)
        if callable(suggest_with_usage):
            return suggest_with_usage(screenshot, context)
        return self._brain.suggest(screenshot, context), BrainUsage()

    def _execute(self, suggestion: FallbackSuggestion) -> bool:
        """执行建议动作；坐标动作在 text 动作失败后才会由模型给出（prompt 约束）。"""
        if suggestion.action == "click_text":
            return bool(self._driver.click_text_contains(suggestion.target))
        if suggestion.action == "click_coords":
            try:
                x_str, y_str = suggestion.target.split(",", 1)
                x_px, y_px = float(x_str), float(y_str)
            except (ValueError, AttributeError):
                return False
            return bool(self._driver.click_at_screenshot_px(x_px, y_px))
        return False
