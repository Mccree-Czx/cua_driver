"use client";

import { useEffect, useState } from "react";
import { Table } from "antd";
import type { ColumnsType } from "antd/es/table";

type TaskLogRow = {
  id: number;
  task_id: string;
  outcome: string;
  attempt: number;
  tokens: number;
  cost: number;
  duration: number;
  note: string | null;
  created_at: string | null;
};

const OUTCOME_CAPSULE: Record<string, string> = {
  success: "status-capsule-qualified",
  failed_needs_manual: "status-capsule-pending",
  failed: "status-capsule-rejected",
  degraded: "status-capsule-running",
};

function OutcomeCapsule({ outcome }: { outcome: string }) {
  return (
    <span className={`capsule-pill ${OUTCOME_CAPSULE[outcome] ?? "status-capsule-rejected"}`}>
      <span className="capsule-pill-dot" />
      {outcome}
    </span>
  );
}

export default function Logs() {
  const [items, setItems] = useState<TaskLogRow[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    fetch("/api/hr/tasklogs?limit=500")
      .then((r) => r.json())
      .then((b) => setItems(b.items ?? []))
      .catch(() => setItems([]))
      .finally(() => setLoading(false));
  }, []);

  const columns: ColumnsType<TaskLogRow> = [
    {
      title: "时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 170,
      render: (v: string | null) => v ?? "—",
    },
    { title: "任务", dataIndex: "task_id", key: "task_id", width: 220, ellipsis: true },
    {
      title: "结果",
      dataIndex: "outcome",
      key: "outcome",
      width: 150,
      render: (v: string) => <OutcomeCapsule outcome={v} />,
    },
    { title: "尝试", dataIndex: "attempt", key: "attempt", width: 60 },
    { title: "tokens", dataIndex: "tokens", key: "tokens", width: 80 },
    {
      title: "耗时(s)",
      dataIndex: "duration",
      key: "duration",
      width: 80,
      render: (v: number) => (v ? Math.round(v) : 0),
    },
    { title: "注记", dataIndex: "note", key: "note", ellipsis: true },
  ];

  return (
    <div className="page talent-page">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Logs</p>
          <h2>运行日志</h2>
          <p>任务执行与成本记录</p>
        </div>
      </div>

      <Table
        rowKey="id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={items}
        pagination={{ pageSize: 50, showSizeChanger: false }}
      />
    </div>
  );
}
