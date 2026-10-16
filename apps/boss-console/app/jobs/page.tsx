"use client";

import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Input, Segmented, Space, Switch, Table, Tag, Tooltip, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import Link from "next/link";

type Job = {
  id: number;
  title: string;
  state: string;          // BOSS 原生：开放中 / 待开放 / 已关闭 / 审核未通过
  status: string;         // 系统侧：active / draft
  details: string;
  jd_complete: boolean;
  tags: string[];
  jd_chars: number;
};

/** BOSS 原生状态色。★ 直接拿 BOSS 的中文当键——服务端就是原样存的。 */
const STATE_COLOR: Record<string, string> = {
  开放中: "green",
  待开放: "gold",
  已关闭: "default",
  审核未通过: "red",
};

export default function JobsPage() {
  const [rows, setRows] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [state, setState] = useState<string>("全部");
  const [q, setQ] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const p = new URLSearchParams();
      if (state !== "全部") p.set("state", state);
      if (q.trim()) p.set("q", q.trim());
      const r = await fetch(`/api/jobs?${p}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setRows((await r.json()).items);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, [state, q]);

  useEffect(() => {
    void load();
  }, [load]);

  async function toggleStatus(job: Job, next: boolean) {
    const status = next ? "active" : "draft";
    const r = await fetch(`/api/hr/jobs/${job.id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ status }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      message.error(d.detail ?? `HTTP ${r.status}`);
      return;
    }
    message.success(next ? "已启用，轮次可以用了" : "已停用，轮次不会再用它");
    await load();
  }

  const columns: ColumnsType<Job> = [
    {
      title: "职位",
      dataIndex: "title",
      width: 240,
      render: (t: string, row: Job) => <Link href={`/jobs/${row.id}`}>{t}</Link>,
    },
    {
      title: "BOSS 状态",
      dataIndex: "state",
      width: 110,
      render: (s: string) => <Tag color={STATE_COLOR[s] ?? "green"}>{s || "开放中"}</Tag>,
    },
    {
      title: "系统启用",
      dataIndex: "status",
      width: 110,
      render: (s: string, row: Job) => (
        // ★ BOSS 让不让我招（state）和系统敢不敢用（status）是两件事。
        //   停用的职位在工具层就看不见——联锁焊在代码里，不在提示词里。
        <Tooltip title={row.jd_complete ? "启用后轮次会用它" : "JD 还没读全，启用也拿不到推荐"}>
          <Switch
            size="small"
            checked={s === "active"}
            checkedChildren="启用"
            unCheckedChildren="停用"
            onChange={(v) => void toggleStatus(row, v)}
          />
        </Tooltip>
      ),
    },
    {
      title: "要求",
      dataIndex: "details",
      render: (d: string) => <Typography.Text type="secondary">{d}</Typography.Text>,
    },
    {
      title: "JD",
      dataIndex: "jd_chars",
      width: 130,
      render: (_: number, row: Job) =>
        row.jd_complete ? (
          <Tag color="blue">{row.jd_chars} 字</Tag>
        ) : (
          // ★ 半截 JD 要显眼：它不能用来打分，但也不能假装没有
          <Tag color="orange">{row.jd_chars} 字 · 未读全</Tag>
        ),
    },
    {
      title: "标签",
      dataIndex: "tags",
      width: 240,
      render: (tags: string[]) => (tags ?? []).slice(0, 2).map((t) => <Tag key={t}>{t}</Tag>),
    },
  ];

  return (
    <>
      <h2 className="page-title">职位</h2>

      <Space style={{ marginBottom: 16 }} wrap>
        <Segmented
          value={state}
          onChange={(v) => setState(String(v))}
          options={["全部", "开放中", "待开放", "已关闭", "审核未通过"]}
        />
        <Input.Search placeholder="搜职位名" allowClear style={{ width: 240 }} onSearch={setQ} />
        <Button onClick={() => void load()}>刷新</Button>
      </Space>

      {err && <Alert type="error" showIcon message={err} style={{ marginBottom: 16 }} />}

      <Table<Job>
        rowKey="id"
        size="middle"
        loading={loading}
        columns={columns}
        dataSource={rows}
        pagination={{ pageSize: 20, showSizeChanger: false }}
      />
      {!loading && rows.length === 0 && !err && (
        <Alert
          style={{ marginTop: 16 }}
          type="info"
          showIcon
          message="还没有数据"
          description="数据由 Mac 上的 agent_boss 采到之后推到 /api/ingest/jobs。"
        />
      )}
    </>
  );
}
