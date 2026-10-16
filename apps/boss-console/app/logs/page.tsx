"use client";

import { useEffect, useState } from "react";
import { Empty, Table } from "antd";
import type { ColumnsType } from "antd/es/table";
import { OutcomeCapsule } from "../status";

/**
 * 运行日志。**结构照猎聘**（`.page talent-page` + page-heading + 表格）。
 *
 * ★ 结果那一列的取值集是 **BOSS 自己的**（`success` / `failed_needs_manual` /
 *   `acked` / `timeout` / `skipped`），跟猎聘不完全一样——猎聘有 `degraded`
 *   （打分服务降级），BOSS 没有打分所以也没有它。映射表在 `status.tsx`
 *   的 `OUTCOME_CAPSULE` 里，**照搬猎聘那张会得到「有值的没颜色」**。
 */
type Row = {
  id: number;
  task_id: string;
  round_name: string;
  outcome: string;
  attempt: number;
  tokens: number;
  cost: number;
  duration: number;
  note: string;
  created_at: string;
};

export default function LogsPage() {
  const [items, setItems] = useState<Row[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch("/api/hr/tasklogs?limit=500")
      .then((r) => r.json())
      .then((b) => setItems(b.items ?? []))
      .catch(() => setItems([]))
      .finally(() => setLoading(false));
  }, []);

  const columns: ColumnsType<Row> = [
    {
      title: "时间", dataIndex: "created_at", key: "created_at", width: 170,
      render: (v: string | null) => (v ? v.replace("T", " ").slice(0, 19) : "—"),
    },
    // ★ 猎聘这一列是 `task_id`（原子任务）。BOSS 的任务粒度是**轮次**，
    //   所以显示轮次名 + task_id——名字比一串 uuid 有用。
    {
      title: "任务", key: "task", width: 240, ellipsis: true,
      render: (_: unknown, r: Row) => (
        <span>
          {r.round_name || "—"}
          <span style={{ color: "var(--text-muted)", fontSize: 12 }}>　{r.task_id}</span>
        </span>
      ),
    },
    {
      title: "结果", dataIndex: "outcome", key: "outcome", width: 150,
      render: (v: string) => <OutcomeCapsule outcome={v} />,
    },
    { title: "尝试", dataIndex: "attempt", key: "attempt", width: 60 },
    { title: "tokens", dataIndex: "tokens", key: "tokens", width: 90 },
    {
      title: "耗时(s)", dataIndex: "duration", key: "duration", width: 90,
      render: (v: number) => (v ? Math.round(v) : 0),
    },
    { title: "注记", dataIndex: "note", key: "note", ellipsis: true, render: (v: string) => v || "—" },
  ];

  return (
    <div className="page talent-page">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Logs</p>
          <h2>运行日志</h2>
          <p>轮次执行与成本记录</p>
        </div>
      </div>

      <Table
        rowKey="id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={items}
        locale={{ emptyText: <Empty description="还没有轮次记录" /> }}
        pagination={{ pageSize: 50, showSizeChanger: false }}
      />
    </div>
  );
}
