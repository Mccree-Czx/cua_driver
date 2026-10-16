"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Alert, Button, Checkbox, Drawer, Empty, Input, Select, Space, Table, Tabs, Tag, Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  REACHABLE_STATUSES, StarRating, StatusCapsule, fmtTime, statusLabel,
} from "../status";

/**
 * 候选人。**结构照猎聘的 `hr-console/app/candidates/page.tsx`**：
 * `.page talent-page` + `.filters` 网格 + Table（点整行开抽屉）+
 * `.talent-drawer` + `.drawer-hero` + Tabs + `.drawer-actions-card`。
 *
 * ★ **BOSS 的抽屉比猎聘多两个 Tab**（「沟通记录」「系统动作」）：
 *   上一轮把独立的「会话」页删了（对齐猎聘的 5 项导航），消息只剩这一个出口。
 *   而猎聘只有「互动时间线」一个——它没有把聊天记录镜像下来。
 */
type Row = {
  id: number;
  status: string;
  job_id: number;
  job_title: string;
  candidate_id: number;
  name: string;
  boss_key: string;
  job_and_company: string;
  work_year: string;
  degree: string;
  age: string;
  summary: string;
  expect_job_city: string;
  expect_salary: string;
  tags: string[];
  last_touch_at: string | null;
};

type Detail = Row & {
  description: string | null;
  company_history: string[];
  /** 系统动作流水 —— **我们对候选人做了什么** */
  interactions: { direction: string; msg_type: string; content: string; sent_at: string }[];
  overrides: { old_status: string; new_status: string; operator: string; reason: string; created_at: string }[];
  /** 会话 —— **双方实际说了什么**。与上面那张表刻意分开。 */
  conversations: {
    id: number;
    job_title: string;
    status: string;
    boss_time: string;
    unread: number;
    messages: { direction: string; text: string }[];
  }[];
};

type JobOption = { id: number; title: string };

/**
 * 「沟通记录」Tab：**双方实际说了什么**（`messages` 的镜像）。
 *
 * ★ 它和「系统动作」是**两件事**，刻意分开：这一栏是聊天记录（唯一可信的原始证据），
 *   那一栏是「我们对候选人做过什么」。混起来会**污染「一人一消息」的联锁判断**——
 *   这也正是后端不把 `messages` 和 `interactions` 合成一张表的理由。
 */
function ChatTab({ conversations }: { conversations: Detail["conversations"] }) {
  if (conversations.length === 0) return <Empty description="还没有沟通记录" />;
  return (
    <>
      {conversations.map((c) => (
        <div key={c.id} style={{ marginBottom: 24 }}>
          <div style={{ marginBottom: 8 }}>
            <Tag>{c.job_title}</Tag>
            {c.status && <Tag>{c.status}</Tag>}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {c.boss_time}
            </Typography.Text>
          </div>
          <div className="chat-preview">
            {c.messages.length === 0 ? (
              <Typography.Text type="secondary">（这个会话里还没有消息）</Typography.Text>
            ) : (
              c.messages.map((m, i) => (
                <div key={i} className={`bubble bubble-${m.direction}`}>
                  <div className="bubble-text">{m.text}</div>
                </div>
              ))
            )}
          </div>
        </div>
      ))}
    </>
  );
}

export default function CandidatesPage() {
  const [rows, setRows] = useState<Row[]>([]);
  const [jobs, setJobs] = useState<JobOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState<string | undefined>();
  const [jobId, setJobId] = useState<string | undefined>();
  const [detail, setDetail] = useState<Detail | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const p = new URLSearchParams();
      if (q.trim()) p.set("q", q.trim());
      if (status) p.set("status", status);
      if (jobId) p.set("job_id", jobId);
      const r = await fetch(`/api/hr/candidates?${p}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setRows((await r.json()).items);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, [q, status, jobId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    fetch("/api/jobs")
      .then((r) => r.json())
      .then((b) => setJobs(b.items ?? []))
      .catch(() => setJobs([]));
  }, []);

  async function openDetail(jcId: number) {
    const r = await fetch(`/api/hr/candidates/${jcId}`);
    if (r.ok) setDetail(await r.json());
  }

  async function review(decision: "approve" | "reject") {
    if (!detail) return;
    const r = await fetch(`/api/hr/candidates/${detail.id}/review`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ decision, note: decision === "approve" ? "复核确认" : "复核推翻" }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      message.error(body.detail ?? `HTTP ${r.status}`);
      return;
    }
    message.success(`复核完成：${statusLabel(body.status)}`);
    await load();
    await openDetail(detail.id);
  }

  const columns: ColumnsType<Row> = [
    {
      title: "候选人",
      key: "name",
      render: (_: unknown, r: Row) => (
        <span className="candidate-name">
          {r.name}
          <small>{r.boss_key}</small>
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
      // ★ BOSS 还没接打分，这一列现在恒为空星——**结构先对齐**，接上就不用改版面。
      title: "评分",
      key: "score",
      width: 110,
      render: () => <StarRating score={null} />,
    },
    {
      title: "简历截图",
      key: "snapshot",
      width: 90,
      render: () => "—",
    },
    {
      title: "最近互动",
      dataIndex: "last_touch_at",
      key: "last_touch_at",
      render: (v: string | null) => fmtTime(v),
    },
  ];

  const detailTabs = detail
    ? [
        {
          key: "basic",
          label: "基本信息",
          children: (
            <div className="detail-grid">
              {[
                ["状态", statusLabel(detail.status)],
                ["职位", detail.job_title],
                ["当前公司·职位", detail.job_and_company || "—"],
                ["经验 / 学历 / 年龄",
                 [detail.work_year, detail.degree, detail.age].filter(Boolean).join(" · ") || "—"],
                ["期望", `${detail.expect_job_city || "—"} · ${detail.expect_salary || "—"}`],
                ["boss_key", detail.boss_key],
              ].map(([k, v]) => (
                <div className="detail-item-box" key={k}>
                  <span className="muted">{k}</span>
                  <div>{v}</div>
                </div>
              ))}
            </div>
          ),
        },
        {
          key: "resume",
          label: "简历资料",
          children: (
            <Space direction="vertical" size="middle" style={{ width: "100%" }}>
              <div>
                <div className="outin-card-subtitle" style={{ marginBottom: 8 }}>自我描述</div>
                <div className="multiline-box">{detail.description || "—"}</div>
              </div>
              <div>
                <div className="outin-card-subtitle" style={{ marginBottom: 8 }}>工作经历</div>
                {(detail.company_history ?? []).length === 0 ? (
                  <span className="muted">—</span>
                ) : (
                  (detail.company_history ?? []).map((c, i) => <div key={i}>{c}</div>)
                )}
              </div>
              <div>
                <div className="outin-card-subtitle" style={{ marginBottom: 8 }}>标签</div>
                {(detail.tags ?? []).length === 0 ? (
                  <span className="muted">—</span>
                ) : (
                  (detail.tags ?? []).map((t) => <Tag key={t}>{t}</Tag>)
                )}
              </div>
            </Space>
          ),
        },
        {
          key: "chat",
          label: `沟通记录${detail.conversations.length ? ` (${detail.conversations.length})` : ""}`,
          children: <ChatTab conversations={detail.conversations} />,
        },
        {
          key: "actions",
          label: "系统动作",
          children: (
            <Space direction="vertical" size="small" style={{ width: "100%" }}>
              <span className="muted" style={{ fontSize: 12 }}>
                这一栏记的是**我们对候选人做过什么**（打招呼 / 求简历 / 收简历）。
                对方说的话在「沟通记录」里——两者刻意分开。
              </span>
              {detail.interactions.length === 0 ? (
                <span className="muted">还没有系统动作</span>
              ) : (
                detail.interactions.map((i, idx) => (
                  <div key={idx}>
                    {i.direction === "out" ? "→" : "←"} {i.msg_type}：{i.content || "—"}
                    <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>
                      {fmtTime(i.sent_at)}
                    </span>
                  </div>
                ))
              )}
              {detail.overrides.length > 0 && (
                <>
                  <div className="outin-card-subtitle" style={{ marginTop: 12 }}>人工复核</div>
                  {detail.overrides.map((o, idx) => (
                    <div key={idx}>
                      {statusLabel(o.old_status)} → {statusLabel(o.new_status)}：{o.reason || "—"}
                    </div>
                  ))}
                </>
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
          <label className="ant-form-item-label"><label>职位</label></label>
          <Select
            style={{ width: "100%" }}
            value={jobId}
            placeholder="全部职位"
            allowClear
            options={jobs.map((j) => ({ value: String(j.id), label: j.title }))}
            onChange={(v) => setJobId(v)}
          />
        </div>
        <div className="ant-form-item">
          <label className="ant-form-item-label"><label>状态</label></label>
          <Select
            style={{ width: "100%" }}
            value={status}
            placeholder="全部状态"
            allowClear
            options={REACHABLE_STATUSES.map((s) => ({ value: s, label: statusLabel(s) }))}
            onChange={(v) => setStatus(v)}
          />
        </div>
        <div className="ant-form-item">
          <label className="ant-form-item-label"><label>搜索</label></label>
          <Input.Search
            placeholder="搜姓名 / 公司"
            allowClear
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onSearch={(v) => setQ(v)}
          />
        </div>
        <div className="ant-form-item" style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {/* ★ 保留但禁用——BOSS 还没接打分。照「结构对齐」的约定留着，
              接上打分时把它打开即可。 */}
          <Checkbox disabled title="BOSS 还没接打分链路，暂时无法按星级筛">
            仅达标分档（≥3 星）
          </Checkbox>
        </div>
      </div>

      {err && <Alert type="error" showIcon message={err} style={{ marginBottom: 16 }} />}

      <Table<Row>
        rowKey="id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={rows}
        onRow={(row) => ({ onClick: () => void openDetail(row.id) })}
        locale={{ emptyText: <Empty description="还没有候选人（采数端扫回来之后会出现在这里）" /> }}
        pagination={{ pageSize: 50, showSizeChanger: false }}
      />

      <Drawer
        className="talent-drawer"
        width={600}
        open={detail !== null}
        onClose={() => setDetail(null)}
        title={detail ? `#${detail.id} ${detail.name}` : ""}
      >
        {detail && (
          <>
            <div className="drawer-hero">
              <div className="drawer-hero-avatar">{detail.name?.[0] ?? "?"}</div>
              <div className="drawer-hero-content">
                <h3>{detail.name}</h3>
                <p>{detail.job_and_company || "（未知公司·职位）"}</p>
                <div className="drawer-hero-tags">
                  <span className="drawer-hero-tag">jc #{detail.id}</span>
                  <StatusCapsule status={detail.status} />
                </div>
              </div>
            </div>
            <Tabs defaultActiveKey="basic" items={detailTabs} />
            {!["closed", "hr_reviewed"].includes(detail.status) && (
              <div className="drawer-actions-card">
                <Space>
                  <Button type="primary" onClick={() => void review("approve")}>
                    复核通过
                  </Button>
                  <Button danger onClick={() => void review("reject")}>
                    复核推翻
                  </Button>
                </Space>
              </div>
            )}
          </>
        )}
      </Drawer>
    </div>
  );
}
