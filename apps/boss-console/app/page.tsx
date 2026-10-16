"use client";

import { useEffect, useState } from "react";
import { Alert, Button, Collapse, Form, Input, Rate, Spin, Table, message } from "antd";
import { statusCapsuleClass, statusLabel } from "./status";

/**
 * 驾驶舱。**结构照猎聘的 `hr-console/app/page.tsx`**：三张 `.outin-card`。
 *
 * 用猎聘那套「视觉积木」（`.page` / `.outin-card` / `.stat-card` /
 * `.capsule-pill`）而不是 antd 原生的 `Card`/`Statistic`——
 * `globals.css` 里这些类**本来就有**（BOSS 那份是猎聘那份的严格超集），
 * 之前只是没用。
 */

type Overview = {
  jobs: { total: number; usable: number; 开放中: number; jd_ready: number };
  candidates: number;
  jc: { total: number; greeted: number; resume_received: number; closed: number };
  status_counts: Record<string, number>;
  conversations: { total: number; unread: number };
  messages: { total: number; in: number; out: number };
  greeted: number;
  today: { quota_used: number; quota_cap: number };
};

type DailyDay = {
  date: string;
  new_jc: number;
  greet_request: number;
  manual: number;
  direct_request: number | null;
  received: number | null;
  conversion: number | null;
  messages_in: number;
  messages_out: number;
};

type Daily = { days: DailyDay[]; totals: Record<string, number | null> };

type ScoringPrefs = {
  min_stars?: number;
  boosters?: string[];
  veto?: string[];
  requirements?: string[];
};

type ScorableJob = {
  job_id: number;
  title: string;
  jd_text: string | null;
  scoring_prefs: ScoringPrefs;
};

/** 照猎聘的 `StatCard`（`hr-console/app/page.tsx:106`）。 */
function StatCard({ label, value }: { label: string; value: number }) {
  return (
    <div className="stat-card">
      <div className="stat-card-accent-bar" />
      <div className="ant-typography-secondary">{label}</div>
      <b>{value}</b>
    </div>
  );
}

/**
 * 评分偏好表单。照猎聘的 `JobScoringForm`，**它是驾驶舱职位卡的一部分，
 * 不是独立一栏**——猎聘的 `/scoring` 页其实是个没人链接的孤儿页。
 *
 * ★ 比猎聘多做一样：**把 JD 一起显示**。加分点脱离了 JD 看没有意义。
 */
function JobScoringForm({ job, onSaved }: { job: ScorableJob; onSaved: () => void }) {
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const p = job.scoring_prefs || {};
    form.setFieldsValue({
      min_stars: p.min_stars ?? 3,
      boosters: (p.boosters || []).join("\n"),
      veto: (p.veto || []).join("\n"),
      requirements: (p.requirements || []).join("\n"),
    });
  }, [job, form]);

  async function onSave(values: {
    min_stars: number; boosters?: string; veto?: string; requirements?: string;
  }) {
    const lines = (s?: string) => (s || "").split("\n").map((x) => x.trim()).filter(Boolean);
    setSaving(true);
    try {
      const r = await fetch(`/api/hr/scoring/${job.job_id}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          scoring_prefs: {
            min_stars: values.min_stars,
            boosters: lines(values.boosters),
            veto: lines(values.veto),
            requirements: lines(values.requirements),
          },
        }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      message.success(`${job.title} 已保存`);
      onSaved();
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Form form={form} layout="vertical" onFinish={onSave}>
      <div style={{ color: "var(--text-secondary)", fontSize: 12, fontWeight: 600, marginBottom: 8 }}>
        招聘信息 / JD
      </div>
      <div className="multiline-box" style={{ marginBottom: 16 }}>
        {job.jd_text || "（暂无 JD）"}
      </div>
      <Form.Item name="min_stars" label="最低主动沟通星级">
        <Rate />
      </Form.Item>
      <Form.Item name="boosters" label="加分点（每行一项）">
        <Input.TextArea rows={2} placeholder="符合则提升评级，如：有实时数仓经验" />
      </Form.Item>
      <Form.Item name="veto" label="一票否决点（每行一项）">
        <Input.TextArea rows={2} placeholder="命中任一直接 1 星，如：无数据开发经验" />
      </Form.Item>
      <Form.Item name="requirements" label="其他要求（每行一项）">
        <Input.TextArea rows={2} placeholder="如：可接受出差" />
      </Form.Item>
      <Button type="primary" htmlType="submit" loading={saving}>
        保存评分偏好
      </Button>
    </Form>
  );
}

export default function DashboardPage() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [daily, setDaily] = useState<Daily | null>(null);
  const [scorable, setScorable] = useState<ScorableJob[]>([]);
  const [err, setErr] = useState<string | null>(null);

  function load() {
    fetch("/api/hr/overview")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setOverview)
      .catch((e) => setErr(String(e)));
    fetch("/api/hr/daily?days=14").then((r) => r.json()).then(setDaily).catch(() => {});
    // ★ 用 `usable_only=true` 而不是猎聘的「取全部再前端过滤」：
    //   「可用职位」的定义（启用 + 在招 + JD 读全）在服务端 hr_tools.job_list_active，
    //   **只该有一份**，不在前端重写。
    fetch("/api/hr/scoring?usable_only=true")
      .then((r) => r.json())
      .then((d) => setScorable(d.items ?? []))
      .catch(() => setScorable([]));
  }

  useEffect(load, []);

  if (err) {
    return (
      <div className="page">
        <Alert
          type="error"
          showIcon
          message="读不到数据"
          description={`${err}。确认 boss-pipeline 起着（uvicorn app.main:app --port 8100）。`}
        />
      </div>
    );
  }
  if (!overview) {
    return (
      <div className="page">
        <Spin />
      </div>
    );
  }

  /** 算不出来的列统一显示「—」——**不硬造数字**。 */
  const dash = (v: number | null) => (v === null || v === undefined ? "—" : v);

  const dailyColumns = [
    { title: "日期", dataIndex: "date", key: "date" },
    { title: "新增", dataIndex: "new_jc", key: "new_jc" },
    {
      title: "直索要", dataIndex: "direct_request", key: "direct_request",
      render: dash,
    },
    { title: "打招呼", dataIndex: "greet_request", key: "greet_request" },
    { title: "收到简历", dataIndex: "received", key: "received", render: dash },
    {
      title: "转化率", dataIndex: "conversion", key: "conversion",
      render: (v: number | null) => (v === null ? "—" : `${v}%`),
    },
    { title: "转人工", dataIndex: "manual", key: "manual" },
  ];

  return (
    <div className="page">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Dashboard</p>
          <h2>驾驶舱</h2>
          <p>招聘漏斗总览与在招职位管理</p>
        </div>
      </div>

      {/* ① 在招职位 —— 评分偏好按岗位独立配置 */}
      <div className="outin-card">
        <div className="outin-card-header">
          <div className="outin-card-header-left">
            <span className="outin-card-icon">💼</span>
            <div>
              <div className="outin-card-title">在招职位</div>
              <div className="outin-card-subtitle">评分偏好按岗位独立配置</div>
            </div>
          </div>
        </div>
        <div className="outin-card-body">
          {scorable.length > 0 ? (
            <Collapse
              ghost
              items={scorable.map((job) => ({
                key: String(job.job_id),
                label: job.title,
                children: <JobScoringForm job={job} onSaved={load} />,
              }))}
            />
          ) : (
            <span style={{ color: "var(--text-muted)", fontSize: 13 }}>
              暂无可用职位（要 系统启用 + BOSS 在招 + JD 读全）。
              没有 JD 的职位不参与——拿它去筛人是浪费。
            </span>
          )}
        </div>
      </div>

      {/* ② 漏斗总览 */}
      <div className="outin-card">
        <div className="outin-card-header">
          <div className="outin-card-header-left">
            <span className="outin-card-icon">📊</span>
            <div className="outin-card-title">漏斗总览</div>
          </div>
        </div>
        <div className="outin-card-body">
          <div className="dashboard-grid">
            <StatCard label="今日打招呼" value={overview.today.quota_used} />
            <StatCard label="今日收到消息" value={overview.messages.in} />
            <StatCard label="会话未读" value={overview.conversations.unread} />
            <StatCard label="候选人总数" value={overview.jc.total} />
          </div>
          <div className="dashboard-status-grid">
            <div>
              <div className="outin-card-subtitle" style={{ marginBottom: 8 }}>
                状态分布
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                {Object.entries(overview.status_counts).map(([k, v]) => (
                  <span key={k} className={`capsule-pill ${statusCapsuleClass(k)}`}>
                    <span className="capsule-pill-dot" />
                    {statusLabel(k)}: {v}
                  </span>
                ))}
              </div>
            </div>
            <div>
              <div className="outin-card-subtitle" style={{ marginBottom: 8 }}>
                评分分布
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                {/* ★ BOSS 还没接打分，所以每一档恒为「—」。
                    保留这一栏是**为了结构一致**——接上打分时不用再改版面。 */}
                {["1星", "2星", "3星", "4星", "5星", "未评分"].map((k) => (
                  <span key={k} className="capsule-pill status-capsule-queued">
                    <span className="capsule-pill-dot" />
                    {k}: {k === "未评分" ? overview.jc.total : "—"}
                  </span>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* ③ 每日漏斗 */}
      <div className="outin-card">
        <div className="outin-card-header">
          <div className="outin-card-header-left">
            <span className="outin-card-icon">📅</span>
            <div className="outin-card-title">每日漏斗（近 14 天）</div>
          </div>
        </div>
        <div className="outin-card-body">
          <Table
            rowKey="date"
            size="small"
            columns={dailyColumns}
            dataSource={daily?.days ?? []}
            pagination={false}
            footer={
              daily
                ? () => (
                    <span style={{ fontSize: 12 }}>
                      合计：新增 {daily.totals.new_jc} · 打招呼 {daily.totals.greet_request} · 转人工{" "}
                      {daily.totals.manual}
                      　｜　BOSS 侧：收到消息 {daily.totals.messages_in} · 发出消息{" "}
                      {daily.totals.messages_out}
                    </span>
                  )
                : undefined
            }
          />
        </div>
      </div>
    </div>
  );
}
