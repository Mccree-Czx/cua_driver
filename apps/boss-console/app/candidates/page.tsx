"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Alert, Button, Descriptions, Drawer, Empty, Input, Select, Space, Table, Tabs, Tag,
  Typography, message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { REACHABLE_STATUSES, STATUS_COLOR, statusLabel } from "../status";

/**
 * 「沟通记录」Tab：**双方实际说了什么**（`messages` 的镜像）。
 *
 * ★ 它和「系统动作」那一栏是**两件事**，刻意分开：
 *   - 这一栏 = 聊天记录（in + out），是唯一可信的原始证据
 *   - 「系统动作」= 我们对候选人做过什么（打招呼 / 求简历 / 收简历）
 *
 *   这也正是后端不把 `messages` 和 `interactions` 合成一张表的理由：
 *   混起来会**污染「一人一消息」的联锁判断**（联锁只该看我们做过什么）。
 */
function ChatTab({ conversations }: { conversations: Detail["conversations"] }) {
  if (conversations.length === 0) {
    return <Empty description="还没有沟通记录" />;
  }
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

/**
 * 候选人。★ **jc 维度**（某人在某个职位下）——BOSS 的牛人推荐按职位，
 * 所以同一个人在不同职位下是两条记录。这与猎聘一致。
 *
 * ★ 这里**没有打分列**：BOSS 的打分链路还没做（用户已定「先不做」）。
 *   猎聘那边的 match_score / judge_reason 在这里是空的。
 */
type Row = {
  id: number;
  status: string;
  job_id: number;
  job_title: string;
  name: string;
  job_and_company: string;
  work_year: string;
  degree: string;
  age: string;
  summary: string;
  expect_job_city: string;
  expect_salary: string;
  tags: string[];
  last_touch_at: string | null;
  resume_requested_at: string | null;
};

type Detail = Row & {
  boss_key: string;
  description: string | null;
  company_history: string[];
  /** 系统动作流水 —— **我们对候选人做了什么** */
  interactions: { direction: string; msg_type: string; content: string; sent_at: string }[];
  overrides: { old_status: string; new_status: string; operator: string; reason: string; created_at: string }[];
  /** 会话 —— **双方实际说了什么**。与上面那张表刻意分开（见详情里的 Tab 说明） */
  conversations: {
    id: number;
    job_title: string;
    status: string;
    boss_time: string;
    unread: number;
    messages: { direction: string; text: string }[];
  }[];
};

export default function CandidatesPage() {
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState<string | undefined>();
  const [open, setOpen] = useState<Detail | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const p = new URLSearchParams();
      if (q.trim()) p.set("q", q.trim());
      if (status) p.set("status", status);
      const r = await fetch(`/api/hr/candidates?${p}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setRows((await r.json()).items);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, [q, status]);

  useEffect(() => {
    void load();
  }, [load]);

  async function openDetail(id: number) {
    const r = await fetch(`/api/hr/candidates/${id}`);
    setOpen(r.ok ? await r.json() : null);
  }

  async function review(id: number, decision: "approve" | "reject") {
    const r = await fetch(`/api/hr/candidates/${id}/review`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ decision, note: "控制台人工复核" }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      message.error(body.detail ?? `HTTP ${r.status}`);
      return;
    }
    message.success(`已${decision === "approve" ? "通过" : "推翻"}：${statusLabel(body.status)}`);
    setOpen(null);
    await load();
  }

  const columns: ColumnsType<Row> = [
    {
      title: "姓名", dataIndex: "name", width: 120,
      render: (n: string, r) => <a onClick={() => void openDetail(r.id)}>{n}</a>,
    },
    {
      title: "状态", dataIndex: "status", width: 130,
      render: (s: string) => <Tag color={STATUS_COLOR[s] ?? "default"}>{statusLabel(s)}</Tag>,
    },
    { title: "职位", dataIndex: "job_title", width: 220, ellipsis: true },
    { title: "当前公司·职位", dataIndex: "job_and_company", ellipsis: true },
    {
      title: "经验 / 学历 / 年龄", width: 190,
      render: (_: unknown, r: Row) => [r.work_year, r.degree, r.age].filter(Boolean).join(" · "),
    },
    {
      title: "期望", width: 190,
      render: (_: unknown, r: Row) => `${r.expect_job_city || "—"} · ${r.expect_salary || "—"}`,
    },
  ];

  return (
    <>
      <h2 className="page-title">候选人</h2>

      <Space style={{ marginBottom: 16 }} wrap>
        <Input.Search placeholder="搜姓名 / 公司" allowClear style={{ width: 240 }} onSearch={setQ} />
        <Select
          placeholder="按状态筛"
          allowClear
          style={{ width: 160 }}
          value={status}
          onChange={setStatus}
          options={REACHABLE_STATUSES.map((s) => ({ value: s, label: statusLabel(s) }))}
        />
        <Button onClick={() => void load()}>刷新</Button>
      </Space>

      {err && <Alert type="error" showIcon message={err} style={{ marginBottom: 16 }} />}

      <Table<Row>
        rowKey="id"
        size="middle"
        loading={loading}
        columns={columns}
        dataSource={rows}
        pagination={{ pageSize: 20, showSizeChanger: false }}
      />

      <Drawer
        open={open !== null}
        onClose={() => setOpen(null)}
        width={560}
        title={open ? `${open.name} · ${open.job_title}` : ""}
        extra={
          open && !["closed", "hr_reviewed"].includes(open.status) ? (
            <Space>
              <Button size="small" onClick={() => void review(open.id, "reject")}>推翻</Button>
              <Button size="small" type="primary" onClick={() => void review(open.id, "approve")}>
                通过
              </Button>
            </Space>
          ) : null
        }
      >
        {open && (
          <Tabs
            defaultActiveKey="basic"
            items={[
              {
                key: "basic",
                label: "基本信息",
                children: (
                  <Descriptions column={1} size="small" bordered>
                    <Descriptions.Item label="状态">
                      <Tag color={STATUS_COLOR[open.status] ?? "default"}>{statusLabel(open.status)}</Tag>
                    </Descriptions.Item>
                    <Descriptions.Item label="boss_key">
                      <Typography.Text code copyable>{open.boss_key}</Typography.Text>
                    </Descriptions.Item>
                    <Descriptions.Item label="当前公司·职位">{open.job_and_company || "—"}</Descriptions.Item>
                    <Descriptions.Item label="经验 / 学历 / 年龄">
                      {[open.work_year, open.degree, open.age].filter(Boolean).join(" · ") || "—"}
                    </Descriptions.Item>
                    <Descriptions.Item label="期望">
                      {open.expect_job_city || "—"} · {open.expect_salary || "—"}
                    </Descriptions.Item>
                    <Descriptions.Item label="职位">{open.job_title}</Descriptions.Item>
                  </Descriptions>
                ),
              },
              {
                key: "resume",
                label: "简历资料",
                children: (
                  <>
                    <h4 style={{ marginTop: 0 }}>自我描述</h4>
                    <Typography.Paragraph style={{ whiteSpace: "pre-wrap" }}>
                      {open.description || "—"}
                    </Typography.Paragraph>
                    <h4>工作经历</h4>
                    {(open.company_history ?? []).length === 0 ? (
                      <Typography.Text type="secondary">—</Typography.Text>
                    ) : (
                      (open.company_history ?? []).map((c, i) => (
                        <div key={i} className="cand-line">{c}</div>
                      ))
                    )}
                    <h4>标签</h4>
                    {(open.tags ?? []).length === 0 ? (
                      <Typography.Text type="secondary">—</Typography.Text>
                    ) : (
                      (open.tags ?? []).map((t) => <Tag key={t} style={{ marginBottom: 4 }}>{t}</Tag>)
                    )}
                  </>
                ),
              },
              {
                key: "chat",
                label: `沟通记录${open.conversations.length ? ` (${open.conversations.length})` : ""}`,
                children: <ChatTab conversations={open.conversations} />,
              },
              {
                // ★ 第 4 个 Tab 是 BOSS 比猎聘**多**的一栏：猎聘只有「互动时间线」
                //   （= 系统动作），而 BOSS 两边都有数据，就该两边都给出来。
                key: "actions",
                label: "系统动作",
                children: (
                  <>
                    <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
                      这一栏记的是**我们对候选人做过什么**（打招呼 / 求简历 / 收简历）。
                      对方说的话在「沟通记录」里——两者刻意分开。
                    </Typography.Paragraph>
                    {open.interactions.length === 0 ? (
                      <Empty description="还没有系统动作" />
                    ) : (
                      open.interactions.map((i, idx) => (
                        <div key={idx} className="cand-line">
                          {i.direction === "out" ? "→" : "←"} {i.msg_type}：{i.content || "—"}
                          <Typography.Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                            {i.sent_at.slice(0, 16).replace("T", " ")}
                          </Typography.Text>
                        </div>
                      ))
                    )}
                    {open.overrides.length > 0 && (
                      <>
                        <h4 style={{ marginTop: 20 }}>人工复核</h4>
                        {open.overrides.map((o, idx) => (
                          <div key={idx} className="cand-line">
                            {statusLabel(o.old_status)} → {statusLabel(o.new_status)}：{o.reason || "—"}
                          </div>
                        ))}
                      </>
                    )}
                  </>
                ),
              },
            ]}
          />
        )}
      </Drawer>
    </>
  );
}
