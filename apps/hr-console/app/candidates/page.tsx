"use client";

import { useCallback, useEffect, useState } from "react";
import { Select, Input, Checkbox, Table, Drawer, Descriptions, Tabs, Tag, Button, Space, Rate, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { STATUS_LABEL, STATUS_COLOR, statusLabel } from "../status";

type CandidateRow = {
  jc_id: number;
  job_id: number;
  job_title: string;
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
  candidate: { name: string; liepin_user_id: string; online_resume_minimal: Record<string, string> | null };
  snapshot_url: string | null;
  snapshot_urls: string[];
  resume_url: string | null;
  resume_html_url: string | null;
  interactions: { direction: string; msg_type: string; content: string | null; sent_at: string | null }[];
};

const STATUS_OPTIONS = [
  { value: "", label: "全部状态" },
  ...Object.entries(STATUS_LABEL).map(([value, label]) => ({ value, label })),
];

function fmtTime(v: string | null): string {
  if (!v) return "—";
  return v.replace("T", " ").slice(0, 16);
}

export default function Candidates() {
  const [rows, setRows] = useState<CandidateRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const [minScoreOnly, setMinScoreOnly] = useState(false);
  const [minStars, setMinStars] = useState(3);
  const [jobId, setJobId] = useState("");
  const [jobs, setJobs] = useState<{ id: number; title: string }[]>([]);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    fetch("/api/jobs")
      .then((r) => r.json())
      .then((body) => setJobs(body ?? []))
      .catch(() => setJobs([]));
  }, []);

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
    if (jobId) params.set("job_id", jobId);
    if (minScoreOnly) params.set("min_score", String(minStars));
    params.set("limit", "50");
    params.set("offset", String((page - 1) * 50));
    setLoading(true);
    fetch(`/api/hr/candidates?${params}`)
      .then((r) => r.json())
      .then((body) => {
        setRows(body.items ?? []);
        setTotal(body.total ?? 0);
      })
      .catch(() => {
        setRows([]);
        setTotal(0);
      })
      .finally(() => setLoading(false));
  }, [status, q, jobId, minScoreOnly, minStars, page]);

  useEffect(() => {
    setPage(1);
  }, [status, q, jobId, minScoreOnly, minStars]);

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
    { title: "职位", dataIndex: "job_title", key: "job_title", render: (v: string) => v || "—" },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      render: (s: string) => <Tag color={STATUS_COLOR[s]}>{statusLabel(s)}</Tag>,
    },
    {
      title: "评分",
      dataIndex: "match_score",
      key: "match_score",
      width: 120,
      render: (v: number | null) => (v ? <Rate disabled value={v} style={{ fontSize: 14 }} /> : "—"),
    },
    {
      title: "截图",
      dataIndex: "has_snapshot",
      key: "has_snapshot",
      width: 70,
      render: (v: boolean) => (v ? "有" : "—"),
    },
    { title: "最近互动", dataIndex: "last_touch_at", key: "last_touch_at", render: (v: string | null) => fmtTime(v) },
  ];

  const detailTabs = detail
    ? [
        {
          key: "basic",
          label: "基本信息",
          children: (
            <Descriptions column={1} size="small">
              <Descriptions.Item label="状态">
                <Tag color={STATUS_COLOR[detail.jc.status]}>{statusLabel(detail.jc.status)}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="评分">
                {detail.jc.match_score ? <Rate disabled value={detail.jc.match_score} style={{ fontSize: 14 }} /> : "—"}
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
              {detail.resume_html_url && (
                <a
                  href={detail.resume_html_url}
                  target="_blank"
                  style={{ display: "inline-block", marginBottom: 8, color: "#1677ff" }}
                >
                  打开简历网页
                </a>
              )}
              {(detail.snapshot_urls?.length ?? 0) > 0 ? (
                detail.snapshot_urls.map((url, i) => (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    key={i}
                    src={url}
                    alt={`简历截图 ${i + 1}`}
                    style={{ width: "100%", borderRadius: 6, border: "1px solid #f0f0f0" }}
                  />
                ))
              ) : detail.snapshot_url ? (
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
                  [{fmtTime(row.sent_at)}] {row.direction}/{row.msg_type}：{row.content}
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
          style={{ width: 200 }}
          value={jobId || undefined}
          placeholder="全部职位"
          allowClear
          options={jobs.map((j) => ({ value: String(j.id), label: j.title }))}
          onChange={(v) => setJobId(v ?? "")}
        />
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
        pagination={{
          current: page,
          pageSize: 50,
          total,
          showSizeChanger: false,
          onChange: (p) => setPage(p),
        }}
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
