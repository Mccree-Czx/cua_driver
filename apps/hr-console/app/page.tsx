"use client";

import { useEffect, useState } from "react";
import { Row, Col, Card, Statistic, Table, Tag, Collapse, Form, Rate, Input, Button, message } from "antd";
import { STATUS_COLOR, statusLabel } from "./status";

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
      <div style={{ color: "rgba(0,0,0,0.45)", fontSize: 13, marginBottom: 8 }}>招聘信息 / JD</div>
      <div style={{ whiteSpace: "pre-line", fontSize: 13, marginBottom: 16, background: "#fafafa", padding: 12, borderRadius: 6 }}>
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
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <Card title="在招职位">
        {activeJobs.length > 0 ? (
          <Collapse
            items={activeJobs.map((job) => ({
              key: String(job.id),
              label: job.title,
              children: <JobScoringForm job={job} onSaved={load} />,
            }))}
          />
        ) : (
          <span style={{ color: "rgba(0,0,0,0.45)", fontSize: 13 }}>暂无在招职位</span>
        )}
      </Card>

      <Card title="漏斗总览">
        {overview ? (
          <>
            <Row gutter={16}>
              <Col span={6}>
                <Statistic title="今日触达（out）" value={overview.today.touches_out} />
              </Col>
              <Col span={6}>
                <Statistic title="今日收到简历" value={overview.today.received} />
              </Col>
              <Col span={6}>
                <Statistic title="今日转人工" value={overview.today.manual} />
              </Col>
              <Col span={6}>
                <Statistic title="候选人总数" value={totalCandidates} />
              </Col>
            </Row>
            <div style={{ marginTop: 16 }}>
              <div style={{ color: "rgba(0,0,0,0.45)", fontSize: 12, marginBottom: 8 }}>状态分布</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                {Object.entries(overview.status_counts).map(([k, v]) => (
                  <Tag key={k} color={STATUS_COLOR[k]}>
                    {statusLabel(k)}: {v}
                  </Tag>
                ))}
              </div>
            </div>
            <div style={{ marginTop: 12 }}>
              <div style={{ color: "rgba(0,0,0,0.45)", fontSize: 12, marginBottom: 8 }}>评分分布</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                {Object.entries(overview.score_buckets).map(([k, v]) => (
                  <Tag key={k}>
                    {k}: {v}
                  </Tag>
                ))}
              </div>
            </div>
          </>
        ) : (
          <span style={{ color: "rgba(0,0,0,0.45)", fontSize: 13 }}>
            加载中…（需 pipeline 在 127.0.0.1:8000 运行）
          </span>
        )}
      </Card>

      <Card title="每日漏斗（近 14 天）">
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
      </Card>

    </div>
  );
}
