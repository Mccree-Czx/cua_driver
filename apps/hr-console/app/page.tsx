"use client";

import { useEffect, useState } from "react";
import { Table, Collapse, Form, Rate, Input, Button, message } from "antd";
import { statusLabel, statusCapsuleClass } from "./status";

type ScoringPrefs = {
  min_stars?: number;
  boosters?: string[];
  veto?: string[];
  requirements?: string[];
};

type Overview = {
  job?: { id: number; title: string; llm_threshold: number } | null;
  status_counts: Record<string, number>;
  score_buckets: Record<string, number>;
  today: { touches_out: number; received: number; received_target?: number; manual: number };
};

type DailyDay = {
  date: string;
  new_jc: number;
  direct_request: number;
  greet_request: number;
  received: number;
  conversion: number | null;
  tokens: number;
  cost: number;
  manual: number;
};

type Job = {
  id: number;
  title: string;
  jd_text: string;
  llm_threshold: number;
  status: string;
  scoring_prefs: ScoringPrefs | null;
};

function JobScoringForm({ job, onSaved }: { job: Job; onSaved: () => void }) {
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const prefs = job.scoring_prefs || {};
    form.setFieldsValue({
      min_stars: prefs.min_stars ?? 3,
      boosters: (prefs.boosters || []).join("\n"),
      veto: (prefs.veto || []).join("\n"),
      requirements: (prefs.requirements || []).join("\n"),
    });
  }, [job, form]);

  async function onSave(values: { min_stars: number; boosters?: string; veto?: string; requirements?: string }) {
    const splitLines = (s?: string) => (s || "").split("\n").map((x) => x.trim()).filter(Boolean);
    const scoring_prefs = {
      min_stars: values.min_stars,
      boosters: splitLines(values.boosters),
      veto: splitLines(values.veto),
      requirements: splitLines(values.requirements),
    };
    setSaving(true);
    const resp = await fetch(`/api/hr/jobs/${job.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scoring_prefs }),
    });
    setSaving(false);
    if (resp.ok) {
      message.success(`岗位 ${job.id} 评分偏好已保存`);
      onSaved();
    } else {
      message.error(`保存失败（HTTP ${resp.status}）`);
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
        <Input.TextArea rows={2} placeholder="符合则提升评级，如：海外销售经验、英语流利" />
      </Form.Item>
      <Form.Item name="veto" label="一票否决点（每行一项）">
        <Input.TextArea rows={2} placeholder="命中任一直接 1 星，如：无 ToB 经验" />
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

function StatCard({ label, value }: { label: string; value: number }) {
  return (
    <div className="stat-card">
      <div className="stat-card-accent-bar" />
      <div className="ant-typography-secondary">{label}</div>
      <b>{value}</b>
    </div>
  );
}

export default function Home() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [daily, setDaily] = useState<{ days: DailyDay[]; totals: Record<string, number | null> } | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);

  function load() {
    fetch("/api/hr/overview").then((r) => r.json()).then(setOverview).catch(() => {});
    fetch("/api/hr/daily?days=14").then((r) => r.json()).then(setDaily).catch(() => {});
    fetch("/api/jobs")
      .then((r) => r.json())
      .then((body) => setJobs(body.items ?? body ?? []))
      .catch(() => {});
  }

  useEffect(load, []);

  const totalCandidates = overview
    ? Object.values(overview.status_counts).reduce((a, b) => a + b, 0)
    : 0;

  const dailyColumns = [
    { title: "日期", dataIndex: "date", key: "date" },
    { title: "新增", dataIndex: "new_jc", key: "new_jc" },
    { title: "直索要", dataIndex: "direct_request", key: "direct_request" },
    { title: "打招呼", dataIndex: "greet_request", key: "greet_request" },
    { title: "收到简历", dataIndex: "received", key: "received" },
    {
      title: "转化率",
      dataIndex: "conversion",
      key: "conversion",
      render: (v: number | null) => (v === null ? "—" : `${v}%`),
    },
    { title: "转人工", dataIndex: "manual", key: "manual" },
  ];

  const activeJobs = jobs.filter((j) => j.status === "active");

  return (
    <div className="page">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Dashboard</p>
          <h2>驾驶舱</h2>
          <p>招聘漏斗总览与在招职位管理</p>
        </div>
      </div>

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
          {activeJobs.length > 0 ? (
            <Collapse
              ghost
              items={activeJobs.map((job) => ({
                key: String(job.id),
                label: job.title,
                children: <JobScoringForm job={job} onSaved={load} />,
              }))}
            />
          ) : (
            <span style={{ color: "var(--text-muted)", fontSize: 13 }}>暂无在招职位</span>
          )}
        </div>
      </div>

      <div className="outin-card">
        <div className="outin-card-header">
          <div className="outin-card-header-left">
            <span className="outin-card-icon">📊</span>
            <div className="outin-card-title">漏斗总览</div>
          </div>
        </div>
        <div className="outin-card-body">
          {overview ? (
            <>
              <div className="dashboard-grid">
                <StatCard label="今日触达（out）" value={overview.today.touches_out} />
                <StatCard label="今日收到简历" value={overview.today.received} />
                <StatCard label="今日转人工" value={overview.today.manual} />
                <StatCard label="候选人总数" value={totalCandidates} />
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
                    {Object.entries(overview.score_buckets).map(([k, v]) => (
                      <span key={k} className="capsule-pill status-capsule-queued">
                        <span className="capsule-pill-dot" />
                        {k}: {v}
                      </span>
                    ))}
                  </div>
                </div>
              </div>
            </>
          ) : (
            <span style={{ color: "var(--text-muted)", fontSize: 13 }}>
              加载中…（需 pipeline 在 127.0.0.1:8000 运行）
            </span>
          )}
        </div>
      </div>

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
                      合计：新增 {daily.totals.new_jc} · 收到简历 {daily.totals.received} · 转化率{" "}
                      {daily.totals.conversion === null ? "—" : `${daily.totals.conversion}%`} · 转人工 {daily.totals.manual}
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
