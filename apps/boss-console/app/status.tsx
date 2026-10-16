// 候选人状态：底层英文技术态 → 前端中文显示映射（只改显示，不改 DB/状态机）
//
// ★ 这份映射与 hr-console 的**逐字一致**——11 态枚举来自共享的
//   `packages/contracts/hr_workbuddy/models.py`，两边前端都要按状态上色，
//   一旦分叉就会各说各话。
//
// ★ 但 BOSS 上有三个态**走不到**（见 services/boss-pipeline/app/state_machine.py）：
//   `screened_pass`（BOSS 推荐流已经筛过一遍，不再插一态）、
//   `rejected_hard` / `rejected_llm`（没有打分链路）。
//   它们保留在映射里是为了**跨端一致**，不是为了显示——BOSS 的数据里不会出现。

export const STATUS_LABEL: Record<string, string> = {
  new: "新候选人",
  screened_pass: "初筛通过",     // BOSS 走不到
  rejected_hard: "硬规则拒绝",   // BOSS 走不到
  rejected_llm: "评分未通过",    // BOSS 走不到
  greeted: "已打招呼",
  resume_requested: "已索要简历",
  awaiting_resume: "等待简历",
  resume_received: "已收到简历",
  no_response: "无回复",
  hr_reviewed: "HR已复核",
  closed: "已关闭",
};

export const STATUS_COLOR: Record<string, string> = {
  new: "blue",
  screened_pass: "geekblue",
  rejected_hard: "red",
  rejected_llm: "volcano",
  greeted: "blue",
  resume_requested: "orange",
  awaiting_resume: "gold",
  resume_received: "green",
  no_response: "default",
  hr_reviewed: "cyan",
  closed: "default",
};

export function statusLabel(status: string): string {
  return STATUS_LABEL[status] ?? status;
}

// 状态 → 胶囊配色类（迁移自 ta.hr.outin 的 status-capsule-*，样式在 globals.css 里）
export const STATUS_CAPSULE: Record<string, string> = {
  new: "status-capsule-queued",
  screened_pass: "status-capsule-qualified",
  rejected_hard: "status-capsule-rejected",
  rejected_llm: "status-capsule-rejected",
  greeted: "status-capsule-contacting",
  resume_requested: "status-capsule-running",
  awaiting_resume: "status-capsule-pending",
  resume_received: "status-capsule-hired",
  no_response: "status-capsule-rejected",
  hr_reviewed: "status-capsule-interview",
  closed: "status-capsule-rejected",
};

export function statusCapsuleClass(status: string): string {
  return STATUS_CAPSULE[status] ?? "status-capsule-rejected";
}

/** BOSS 上真正会出现的那些态（给筛选下拉用）。 */
export const REACHABLE_STATUSES = [
  "new", "greeted", "resume_requested", "awaiting_resume",
  "resume_received", "no_response", "hr_reviewed", "closed",
] as const;

/**
 * 轮次结果 → 胶囊配色。
 *
 * ★ 这张表是 **BOSS 自己的**——猎聘那份（`success/failed_needs_manual/failed/degraded`）
 *   跟 BOSS 的取值集不一样：BOSS 没有 `degraded`（那是打分服务的降级态），
 *   但多了 `acked`（人工销过账）、`timeout`（轮次超时）、`skipped`（闸门拦下）。
 *   照搬会得到「有值的没颜色、有颜色的没值」。
 */
export const OUTCOME_CAPSULE: Record<string, string> = {
  success: "status-capsule-qualified",
  failed_needs_manual: "status-capsule-pending",
  acked: "status-capsule-running",
  timeout: "status-capsule-pending",
  skipped: "status-capsule-queued",
  failed: "status-capsule-rejected",
};

export function outcomeCapsuleClass(outcome: string): string {
  return OUTCOME_CAPSULE[outcome] ?? "status-capsule-rejected";
}

/** 胶囊：状态。★ 全站统一用它，不再用 antd `<Tag>`（对齐猎聘的观感）。 */
export function StatusCapsule({ status }: { status: string }) {
  return (
    <span className={`capsule-pill ${statusCapsuleClass(status)}`}>
      <span className="capsule-pill-dot" />
      {statusLabel(status)}
    </span>
  );
}

/** 胶囊：轮次结果。 */
export function OutcomeCapsule({ outcome }: { outcome: string }) {
  return (
    <span className={`capsule-pill ${outcomeCapsuleClass(outcome)}`}>
      <span className="capsule-pill-dot" />
      {outcome}
    </span>
  );
}

/** 星级。★ BOSS 还没打分，所以现在只会渲染出一排空位——**结构先对齐**。 */
export function StarRating({ score }: { score: number | null }) {
  if (!score) return <span className="stars">{"☆☆☆☆☆"}</span>;
  return (
    <span className="stars">
      {"★".repeat(score)}
      {"☆".repeat(Math.max(0, 5 - score))}
    </span>
  );
}

/** 时间戳统一格式（猎聘用的是 `T`→空格、截到分）。 */
export function fmtTime(v: string | null | undefined): string {
  if (!v) return "—";
  return v.replace("T", " ").slice(0, 16);
}
