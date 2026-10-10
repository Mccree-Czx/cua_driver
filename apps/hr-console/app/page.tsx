"use client";

import { useEffect, useState } from "react";
import { Row, Col, Card, Statistic, Table, Tag, InputNumber, Button, message } from "antd";

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
  reply: number;
  received: number;
  conversion: number | null;
  tokens: number;
  cost: number;
  manual: number;
};

type Job = { id: number; title: string; llm_threshold: number; status: string };

const STATUS_COLOR: Record<string, string> = {
  rejected_hard: "red",
  rejected_llm: "volcano",
  resume_received: "green",
  hr_reviewed: "cyan",
  closed: "default",
  new: "blue",
};

export default function Home() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [daily, setDaily] = useState<{ days: DailyDay[]; totals: Record<string, number | null> } | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [thresholdDraft, setThresholdDraft] = useState<Record<number, number>>({});

  function load() {
    fetch("/api/hr/overview").then((r) => r.json()).then(setOverview).catch(() => {});
    fetch("/api/hr/daily?days=14").then((r) => r.json()).then(setDaily).catch(() => {});
    fetch("/api/jobs")
      .then((r) => r.json())
      .then((body) => setJobs(body.items ?? body ?? []))
      .catch(() => {});
  }

  useEffect(load, []);

  async function saveThreshold(jobId: number) {
    const value = thresholdDraft[jobId];
    if (value === undefined) return;
    const resp = await fetch(`/api/hr/jobs/${jobId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ llm_threshold: value }),
    });
    if (resp.ok) message.success(`岗位 ${jobId} 阈值已更新为 ${value}`);
    else message.error(`更新失败（HTTP ${resp.status}）`);
    load();
  }

  const totalCandidates = overview
    ? Object.values(overview.status_counts).reduce((a, b) => a + b, 0)
    : 0;

  const dailyColumns = [
    { title: "日期", dataIndex: "date", key: "date" },
    { title: "新增", dataIndex: "new_jc", key: "new_jc" },
    { title: "直索要", dataIndex: "direct_request", key: "direct_request" },
    { title: "打招呼", dataIndex: "greet_request", key: "greet_request" },
    { title: "回执", dataIndex: "reply", key: "reply" },
    { title: "收到简历", dataIndex: "received", key: "received" },
    {
      title: "转化率",
      dataIndex: "conversion",
      key: "conversion",
      render: (v: number | null) => (v === null ? "—" : `${v}%`),
    },
    { title: "tokens", dataIndex: "tokens", key: "tokens" },
    { title: "转人工", dataIndex: "manual", key: "manual" },
  ];

  const jobColumns = [
    { title: "岗位", dataIndex: "title", key: "title", render: (_: string, r: Job) => `#${r.id} ${r.title}（${r.status}）` },
    {
      title: "LLM 阈值",
      dataIndex: "llm_threshold",
      key: "llm_threshold",
      render: (_: number, r: Job) => (
        <InputNumber
          min={0}
          max={100}
          value={thresholdDraft[r.id] ?? r.llm_threshold}
          onChange={(v) => setThresholdDraft({ ...thresholdDraft, [r.id]: v ?? 0 })}
        />
      ),
    },
    {
      title: "操作",
      key: "action",
      render: (_: unknown, r: Job) => (
        <Button type="primary" size="small" onClick={() => saveThreshold(r.id)}>
          保存
        </Button>
      ),
    },
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <Card title="漏斗总览">
        {overview ? (
          <>
            <Row gutter={16}>
              <Col span={6}>
                <Statistic title="今日触达（out）" value={overview.today.touches_out} />
              </Col>
              <Col span={6}>
                <Statistic title="今日收到简历" value={overview.today.received} suffix={`/ ${overview.today.received_target ?? 50}`} />
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
                    {k}: {v}
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
                    {daily.totals.conversion === null ? "—" : `${daily.totals.conversion}%`} · tokens{" "}
                    {daily.totals.tokens} · 转人工 {daily.totals.manual}
                  </span>
                )
              : undefined
          }
        />
      </Card>

      <Card title="岗位阈值调整（阈值回流）">
        <Table rowKey="id" size="small" columns={jobColumns} dataSource={jobs} pagination={false} />
      </Card>
    </div>
  );
}
