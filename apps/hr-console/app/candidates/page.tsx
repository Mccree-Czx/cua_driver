"use client";

import { useCallback, useEffect, useState } from "react";
import { Select, Input, Checkbox, Table, Drawer, Descriptions, Tabs, Tag, Button, Space, Rate, message } from "antd";
import type { ColumnsType } from "antd/es/table";

type CandidateRow = {
  jc_id: number;
  name: string;
  liepin_user_id: string;
  source: string;
  status: string;
  match_score: number | null;
  judge_reason: string | null;
  has_snapshot: boolean;
  has_pdf: boolean;
  last_touch_at: string | null;
};

type Detail = {
  jc: { id: number; status: string; match_score: number | null; judge_reason: string | null };
  candidate: { name: string; liepin_user_id: string; online_resume_minimal: Record<string, string> | null; resume_ocr_text: string | null };
  snapshot_url: string | null;
  resume_url: string | null;
  interactions: { direction: string; msg_type: string; content: string | null; sent_at: string | null }[];
};

const STATUS_OPTIONS = [
  { value: "", label: "全部状态" },
  { value: "new", label: "new" },
  { value: "screened_pass", label: "screened_pass" },
  { value: "resume_requested", label: "resume_requested" },
  { value: "awaiting_resume", label: "awaiting_resume" },
  { value: "resume_received", label: "resume_received" },
  { value: "hr_reviewed", label: "hr_reviewed" },
  { value: "no_response", label: "no_response" },
  { value: "closed", label: "closed" },
  { value: "rejected_hard", label: "rejected_hard" },
  { value: "rejected_llm", label: "rejected_llm" },
];

const STATUS_COLOR: Record<string, string> = {
  rejected_hard: "red",
  rejected_llm: "volcano",
  resume_received: "green",
  hr_reviewed: "cyan",
  closed: "default",
  new: "blue",
  screened_pass: "geekblue",
  awaiting_resume: "gold",
  resume_requested: "orange",
  no_response: "default",
};

export default function Candidates() {
  const [rows, setRows] = useState<CandidateRow[]>([]);
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const [minScoreOnly, setMinScoreOnly] = useState(false);
  const [minStars, setMinStars] = useState(3);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    fetch("/api/hr/overview")
      .then((r) => r.json())
      .then((body) => {
        const prefs = body?.job?.scoring_prefs || {};
        if (prefs.min_stars) setMinStars(prefs.min_stars);
      })
      .catch(() => {});
  }, []);

  const load = useCallback(() => {
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    if (q) params.set("q", q);
    if (minScoreOnly) params.set("min_score", String(minStars));
    setLoading(true);
    fetch(`/api/hr/candidates?${params}`)
      .then((r) => r.json())
      .then((body) => setRows(body.items ?? []))
      .catch(() => setRows([]))
      .finally(() => setLoading(false));
  }, [status, q, minScoreOnly, minStars]);

  useEffect(load, [load]);

  async function openDetail(jcId: number) {
    const resp = await fetch(`/api/hr/candidates/${jcId}`);
    if (resp.ok) setDetail(await resp.json());
  }

  async function review(decision: "approve" | "reject") {
    if (!detail) return;
    const resp = await fetch(`/api/hr/candidates/${detail.jc.id}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, note: decision === "approve" ? "复核确认" : "复核推翻" }),
    });
    const body = await resp.json().catch(() => ({}));
    if (resp.ok) message.success(`复核完成：${body.status}`);
    else message.error(`复核失败（HTTP ${resp.status}）`);
    load();
    openDetail(detail.jc.id);
  }

  async function rerun() {
    if (!detail) return;
    const resp = await fetch(`/api/hr/candidates/${detail.jc.id}/rerun`, { method: "POST" });
    const body = await resp.json().catch(() => ({}));
    if (resp.ok) message.success(`已派发重跑：${body.type}`);
    else message.error(`重跑失败（HTTP ${resp.status}：${body.detail ?? ""}）`);
  }

  const columns: ColumnsType<CandidateRow> = [
    { title: "jc", dataIndex: "jc_id", key: "jc_id", width: 60 },
    { title: "姓名", dataIndex: "name", key: "name" },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      render: (s: string) => <Tag color={STATUS_COLOR[s]}>{s}</Tag>,
    },
    {
      title: "评分",
      dataIndex: "match_score",
      key: "match_score",
      width: 120,
      render: (v: number | null) => (v ? <Rate disabled value={v} /> : "—"),
    },
    {
      title: "截图",
      dataIndex: "has_snapshot",
      key: "has_snapshot",
      width: 70,
      render: (v: boolean) => (v ? "有" : "—"),
    },
    {
      title: "PDF",
      dataIndex: "has_pdf",
      key: "has_pdf",
      width: 70,
      render: (v: boolean) => (v ? "有" : "—"),
    },
    { title: "最近触达", dataIndex: "last_touch_at", key: "last_touch_at", render: (v: string | null) => v ?? "—" },
  ];

  const detailTabs = detail
    ? [
        {
          key: "basic",
          label: "基本信息",
          children: (
            <Descriptions column={1} size="small">
              <Descriptions.Item label="状态">
                <Tag color={STATUS_COLOR[detail.jc.status]}>{detail.jc.status}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="评分">
                {detail.jc.match_score ? <Rate disabled value={detail.jc.match_score} /> : "—"}
              </Descriptions.Item>
              <Descriptions.Item label="judge_reason">{detail.jc.judge_reason ?? "—"}</Descriptions.Item>
              <Descriptions.Item label="liepin_user_id">{detail.candidate.liepin_user_id}</Descriptions.Item>
            </Descriptions>
          ),
        },
        {
          key: "resume",
          label: "简历资料",
          children: (
            <Space direction="vertical" style={{ width: "100%" }}>
              {detail.snapshot_url ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={detail.snapshot_url} alt="snapshot" style={{ width: "100%", borderRadius: 6, border: "1px solid #f0f0f0" }} />
              ) : (
                <span style={{ color: "rgba(0,0,0,0.45)", fontSize: 12 }}>无快照</span>
              )}
              {detail.resume_url ? (
                <a href={detail.resume_url} target="_blank">
                  打开简历 PDF（预签名）
                </a>
              ) : (
                <span style={{ color: "rgba(0,0,0,0.45)", fontSize: 12 }}>未收到附件</span>
              )}
              <div style={{ color: "rgba(0,0,0,0.45)", fontSize: 12, marginTop: 8 }}>在线简历 7 字段</div>
              <pre style={{ background: "#fafafa", padding: 12, borderRadius: 6, fontSize: 12 }}>
                {JSON.stringify(detail.candidate.online_resume_minimal ?? {}, null, 2)}
              </pre>
              {detail.candidate.resume_ocr_text && (
                <>
                  <div style={{ color: "rgba(0,0,0,0.45)", fontSize: 12, marginTop: 8 }}>简历文本（OCR）</div>
                  <pre
                    style={{
                      background: "#fafafa",
                      padding: 12,
                      borderRadius: 6,
                      fontSize: 12,
                      whiteSpace: "pre-wrap",
                      maxHeight: 400,
                      overflowY: "auto",
                    }}
                  >
                    {detail.candidate.resume_ocr_text}
                  </pre>
                </>
              )}
            </Space>
          ),
        },
        {
          key: "timeline",
          label: "互动时间线",
          children: (
            <Space direction="vertical" style={{ width: "100%" }}>
              {detail.interactions.map((row, i) => (
                <div key={i} style={{ background: "#fafafa", padding: "6px 10px", borderRadius: 6, fontSize: 12 }}>
                  [{row.sent_at}] {row.direction}/{row.msg_type}：{row.content}
                </div>
              ))}
              {detail.interactions.length === 0 && <span style={{ color: "rgba(0,0,0,0.45)", fontSize: 12 }}>无互动</span>}
            </Space>
          ),
        },
      ]
    : [];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <Space wrap>
        <Select
          style={{ width: 160 }}
          value={status}
          options={STATUS_OPTIONS}
          onChange={(v) => setStatus(v)}
        />
        <Input.Search
          placeholder="搜姓名 / liepin id"
          allowClear
          style={{ width: 240 }}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onSearch={(v) => setQ(v)}
        />
        <Checkbox checked={minScoreOnly} onChange={(e) => setMinScoreOnly(e.target.checked)}>
          仅达标分档（≥{minStars} 星）
        </Checkbox>
        <span style={{ color: "rgba(0,0,0,0.45)", fontSize: 12 }}>{rows.length} 行</span>
      </Space>

      <Table
        rowKey="jc_id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={rows}
        onRow={(row) => ({ onClick: () => openDetail(row.jc_id), style: { cursor: "pointer" } })}
        pagination={{ pageSize: 50, showSizeChanger: false }}
      />

      <Drawer
        width={600}
        open={!!detail}
        onClose={() => setDetail(null)}
        title={detail ? `#${detail.jc.id} ${detail.candidate.name}` : ""}
      >
        {detail && (
          <>
            <Tabs defaultActiveKey="basic" items={detailTabs} />
            <Space style={{ marginTop: 16 }}>
              <Button type="primary" onClick={() => review("approve")}>
                复核通过
              </Button>
              <Button danger onClick={() => review("reject")}>
                复核推翻
              </Button>
              <Button onClick={rerun}>手动重跑</Button>
            </Space>
          </>
        )}
      </Drawer>
    </div>
  );
}
