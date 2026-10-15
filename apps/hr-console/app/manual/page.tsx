"use client";

import { useCallback, useEffect, useState } from "react";
import { Table, Button, Space, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { statusLabel, statusCapsuleClass } from "../status";

type QueueItem = {
  task_id: string;
  task_type: string | null;
  job_candidate_id: number | null;
  candidate_name: string | null;
  jc_status: string | null;
  attempt: number;
  note: string | null;
  created_at: string | null;
};

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

export default function ManualQueue() {
  const [items, setItems] = useState<QueueItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);

  const load = useCallback(() => {
    fetch(`/api/hr/manual-queue?limit=50&offset=${(page - 1) * 50}`)
      .then((r) => r.json())
      .then((body) => {
        setItems(body.items ?? []);
        setTotal(body.total ?? 0);
      })
      .catch(() => {
        setItems([]);
        setTotal(0);
      });
  }, [page]);

  useEffect(load, [load]);

  async function ack(taskId: string) {
    const resp = await fetch(`/api/hr/manual-queue/${taskId}/ack`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note: "工作台已确认" }),
    });
    if (resp.ok) message.success("已注记（acked）");
    else message.error(`注记失败（HTTP ${resp.status}）`);
    load();
  }

  // ★ 「重跑」按钮已于 2026-10-15 **移除**：它调的
  // `POST /api/hr/candidates/{jc_id}/rerun` 端点从来不存在（点了只弹 404）。
  // 不补它的理由见 candidates/page.tsx 里同一处的说明——入队没有消费者，
  // 补上只会变成「按钮说已派发、实际没动」，比 404 更糟。

  const columns: ColumnsType<QueueItem> = [
    { title: "时间", dataIndex: "created_at", key: "created_at", width: 160, render: (v: string | null) => fmtTime(v) },
    { title: "任务类型", dataIndex: "task_type", key: "task_type", render: (v: string | null) => v ?? "（登记过期）" },
    {
      title: "候选人",
      key: "candidate",
      render: (_: unknown, r: QueueItem) =>
        `${r.candidate_name ?? "—"}${r.job_candidate_id !== null ? `（jc=${r.job_candidate_id}）` : ""}`,
    },
    { title: "状态", dataIndex: "jc_status", key: "jc_status", render: (v: string | null) => (v ? <StatusCapsule status={v} /> : "—") },
    { title: "注记", dataIndex: "note", key: "note", render: (v: string | null) => v ?? "—" },
    {
      title: "操作",
      key: "action",
      render: (_: unknown, r: QueueItem) => (
        <Space>
          <Button size="small" onClick={() => ack(r.task_id)}>
            忽略
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <div className="page talent-page">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Manual Queue</p>
          <h2>人工队列</h2>
          <p>需要人工介入处理的任务</p>
        </div>
      </div>

      <div style={{ color: "var(--text-secondary)", fontSize: 12, marginBottom: 12 }}>
        {items.length} 项（failed_needs_manual 落账）
      </div>

      <Table
        rowKey="task_id"
        size="small"
        columns={columns}
        dataSource={items}
        pagination={{ current: page, pageSize: 50, total, showSizeChanger: false, onChange: (p) => setPage(p) }}
      />
    </div>
  );
}
