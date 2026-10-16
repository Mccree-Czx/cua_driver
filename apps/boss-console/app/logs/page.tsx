"use client";

import { useEffect, useState } from "react";
import { Alert, Empty, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";

/** 轮次运行记录。纯只读——猎聘那边也是只读表。 */
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

/** 结果色。★ `failed_needs_manual` 与 `acked` 要区分开：前者还没人管。 */
const OUTCOME_COLOR: Record<string, string> = {
  success: "green",
  failed_needs_manual: "red",
  acked: "blue",
  timeout: "orange",
  skipped: "default",
};

export default function LogsPage() {
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/hr/tasklogs")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d) => setRows(d.items))
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));
  }, []);

  const columns: ColumnsType<Row> = [
    {
      title: "时间", dataIndex: "created_at", width: 170,
      render: (t: string) => t.slice(0, 19).replace("T", " "),
    },
    { title: "轮次", dataIndex: "round_name", width: 140 },
    {
      title: "结果", dataIndex: "outcome", width: 160,
      render: (o: string) => <Tag color={OUTCOME_COLOR[o] ?? "default"}>{o}</Tag>,
    },
    { title: "轮次次数", dataIndex: "attempt", width: 100 },
    {
      title: "耗时", dataIndex: "duration", width: 100,
      render: (d: number) => (d ? `${d.toFixed(1)}s` : "—"),
    },
    {
      title: "tokens / 花费", width: 160,
      render: (_: unknown, r: Row) =>
        r.tokens || r.cost ? `${r.tokens} / ¥${r.cost.toFixed(4)}` : "—",
    },
    {
      title: "说明", dataIndex: "note", ellipsis: true,
      render: (n: string) => <Typography.Text type="secondary">{n || "—"}</Typography.Text>,
    },
  ];

  return (
    <>
      <h2 className="page-title">运行日志</h2>
      {err && <Alert type="error" showIcon message={err} style={{ marginBottom: 16 }} />}
      {!loading && rows.length === 0 && !err && <Empty description="还没有轮次记录" />}
      {rows.length > 0 && (
        <Table<Row>
          rowKey="id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={rows}
          pagination={{ pageSize: 50, showSizeChanger: false }}
        />
      )}
    </>
  );
}
