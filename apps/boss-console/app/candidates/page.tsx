"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Alert, Button, Descriptions, Drawer, Input, Select, Space, Table, Tag, Typography, message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { REACHABLE_STATUSES, STATUS_COLOR, statusLabel } from "../status";

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
  interactions: { direction: string; msg_type: string; content: string; sent_at: string }[];
  overrides: { old_status: string; new_status: string; operator: string; reason: string; created_at: string }[];
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
          <>
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
              <Descriptions.Item label="工作经历">
                {(open.company_history ?? []).join(" / ") || "—"}
              </Descriptions.Item>
              <Descriptions.Item label="自我描述">
                {open.description || "—"}
              </Descriptions.Item>
            </Descriptions>

            <h4 style={{ marginTop: 20 }}>系统动作流水</h4>
            {/* ★ 这里**只有系统做过什么**，不含对方说的话——那是 /conversations 的事。
                两者刻意分开：把对方说的话并进安全账本会污染「一人一消息」的联锁判断。 */}
            {open.interactions.length === 0 ? (
              <Typography.Text type="secondary">（还没有系统动作）</Typography.Text>
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
        )}
      </Drawer>
    </>
  );
}
