// 候选人状态：底层英文技术态 → 前端中文显示映射（只改显示，不改 DB/状态机）

export const STATUS_LABEL: Record<string, string> = {
  new: "新候选人",
  screened_pass: "初筛通过",
  rejected_hard: "硬规则拒绝",
  rejected_llm: "评分未通过",
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

// 状态 → HR Portal 胶囊配色类（迁移自 ta.hr.outin 的 status-capsule-*）
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

