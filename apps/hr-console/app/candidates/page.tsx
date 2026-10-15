"use client";

import { useCallback, useEffect, useState } from "react";
import { Select, Input, Checkbox, Table, Drawer, Tabs, Button, Space, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { STATUS_LABEL, statusLabel, statusCapsuleClass } from "../status";

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

function StatusCapsule({ status }: { status: string }) {
  return (
    <span className={`capsule-pill ${statusCapsuleClass(status)}`}>
      <span className="capsule-pill-dot" />
      {statusLabel(status)}
    </span>
  );
}

function StarRating({ score }: { score: number | null }) {
  if (score == null || score === 0) return <span className="muted">—</span>;
  return (
    <span className="stars" style={{ fontSize: 13 }}>
      {[1, 2, 3, 4, 5].map((i) => (i <= score ? <span key={i}>★</span> : <i key={i}>★</i>))}
    </span>
  );
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

  // ★ 「手动重跑」按钮已于 2026-10-15 **移除**。
  //
  // 它调的 `POST /api/hr/candidates/{jc_id}/rerun` **这个端点从来不存在**，
  // 点了只会弹「重跑失败（HTTP 404）」。
  //
  // 为什么不补上它：那个按钮的语义是「把任务重新入队」——而队列（arq）的消费者
  // 两代前就随旧编排层删掉了。补一个只会**入队到没人读的地方**，按钮报「已派发」，
  // 实际什么都没发生。**那比 404 更糟**：404 至少是诚实的。
  //
  // 真要做「重跑某个候选人」，需要的是 Pi 侧的机制（在下一轮里优先处理这个 jc），
  // 那是一个功能，不是补一个端点。

  const columns: ColumnsType<CandidateRow> = [
    {
      title: "候选人",
      key: "name",
      render: (_: unknown, r: CandidateRow) => (
        <span className="candidate-name">
          {r.name}
          <small>{r.liepin_user_id}</small>
        </span>
      ),
    },
    { title: "职位", dataIndex: "job_title", key: "job_title", render: (v: string) => v || "—" },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      render: (s: string) => <StatusCapsule status={s} />,
    },
    {
      title: "评分",
      dataIndex: "match_score",
      key: "match_score",
      width: 110,
      render: (v: number | null) => <StarRating score={v} />,
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
            <div className="detail-grid">
              <div className="detail-item-box">
                <label>状态</label>
                <p>
                  <StatusCapsule status={detail.jc.status} />
                </p>
              </div>
              <div className="detail-item-box">
                <label>评分</label>
                <p>
                  <StarRating score={detail.jc.match_score} />
                </p>
              </div>
              <div className="detail-item-box" style={{ gridColumn: "1 / -1" }}>
                <label>judge_reason</label>
                <p>{detail.jc.judge_reason ?? "—"}</p>
              </div>
              <div className="detail-item-box" style={{ gridColumn: "1 / -1" }}>
                <label>liepin_user_id</label>
                <p>{detail.candidate.liepin_user_id}</p>
              </div>
            </div>
          ),
        },
        {
          key: "resume",
          label: "简历资料",
          children: (
            <Space direction="vertical" style={{ width: "100%" }}>
              {(detail.snapshot_urls?.length ?? 0) > 0 ? (
                detail.snapshot_urls.map((url, i) => (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    key={i}
                    src={url}
                    alt={`简历截图 ${i + 1}`}
                    style={{ width: "100%", borderRadius: 8, border: "1px solid var(--subtle-border)" }}
                  />
                ))
              ) : detail.snapshot_url ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={detail.snapshot_url}
                  alt="snapshot"
                  style={{ width: "100%", borderRadius: 8, border: "1px solid var(--subtle-border)" }}
                />
              ) : (
                <span style={{ color: "var(--text-muted)", fontSize: 12 }}>无快照</span>
              )}
              {detail.resume_url ? (
                <a href={detail.resume_url} target="_blank" style={{ color: "var(--brand-orange)" }}>
                  打开简历 PDF（预签名）
                </a>
              ) : (
                <span style={{ color: "var(--text-muted)", fontSize: 12 }}>未收到附件</span>
              )}
              <div style={{ color: "var(--text-secondary)", fontSize: 12, marginTop: 8 }}>在线简历 7 字段</div>
              <pre className="multiline-box" style={{ margin: 0 }}>
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
                <div key={i} className="detail-item-box">
                  <label>
                    {fmtTime(row.sent_at)} · {row.direction}/{row.msg_type}
                  </label>
                  <p>{row.content}</p>
                </div>
              ))}
              {detail.interactions.length === 0 && (
                <span style={{ color: "var(--text-muted)", fontSize: 12 }}>无互动</span>
              )}
            </Space>
          ),
        },
      ]
    : [];

  return (
    <div className="page talent-page">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Talent</p>
          <h2>候选人</h2>
          <p>候选人库、筛选与复核</p>
        </div>
      </div>

      <div className="filters">
        <div className="ant-form-item">
          <label className="ant-form-item-label">
            <label>职位</label>
          </label>
          <Select
            style={{ width: "100%" }}
            value={jobId || undefined}
            placeholder="全部职位"
            allowClear
            options={jobs.map((j) => ({ value: String(j.id), label: j.title }))}
            onChange={(v) => setJobId(v ?? "")}
          />
        </div>
        <div className="ant-form-item">
          <label className="ant-form-item-label">
            <label>状态</label>
          </label>
          <Select style={{ width: "100%" }} value={status} options={STATUS_OPTIONS} onChange={(v) => setStatus(v)} />
        </div>
        <div className="ant-form-item">
          <label className="ant-form-item-label">
            <label>搜索</label>
          </label>
          <Input.Search
            placeholder="搜姓名 / liepin id"
            allowClear
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onSearch={(v) => setQ(v)}
          />
        </div>
        <div className="ant-form-item" style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Checkbox checked={minScoreOnly} onChange={(e) => setMinScoreOnly(e.target.checked)}>
            仅达标分档（≥{minStars} 星）
          </Checkbox>
        </div>
      </div>

      <Table
        rowKey="jc_id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={rows}
        onRow={(row) => ({ onClick: () => openDetail(row.jc_id) })}
        pagination={{
          current: page,
          pageSize: 50,
          total,
          showSizeChanger: false,
          onChange: (p) => setPage(p),
        }}
      />

      <Drawer
        className="talent-drawer"
        width={600}
        open={!!detail}
        onClose={() => setDetail(null)}
        title={detail ? `#${detail.jc.id} ${detail.candidate.name}` : ""}
      >
        {detail && (
          <>
            <div className="drawer-hero">
              <div className="drawer-hero-avatar">{detail.candidate.name?.[0] ?? "?"}</div>
              <div className="drawer-hero-content">
                <h3>{detail.candidate.name}</h3>
                <p>{detail.candidate.liepin_user_id}</p>
                <div className="drawer-hero-tags">
                  <span className="drawer-hero-tag">jc #{detail.jc.id}</span>
                  <StatusCapsule status={detail.jc.status} />
                </div>
              </div>
            </div>
            <Tabs defaultActiveKey="basic" items={detailTabs} />
            <div className="drawer-actions-card">
              <Space>
                <Button type="primary" onClick={() => review("approve")}>
                  复核通过
                </Button>
                <Button danger onClick={() => review("reject")}>
                  复核推翻
                </Button>
              </Space>
            </div>
          </>
        )}
      </Drawer>
    </div>
  );
}
