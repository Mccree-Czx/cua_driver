"use client";

import { useCallback, useEffect, useState } from "react";
import { Table, Button, Space, message } from "antd";
import type { ColumnsType } from "antd/es/table";

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

export default function ManualQueue() {
  const [items, setItems] = useState<QueueItem[]>([]);

  const load = useCallback(() => {
    fetch("/api/hr/manual-queue")
      .then((r) => r.json())
      .then((body) => setItems(body.items ?? []))
      .catch(() => setItems([]));
  }, []);

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

  async function rerun(jcId: number | null) {
    if (jcId === null) return;
    const resp = await fetch(`/api/hr/candidates/${jcId}/rerun`, { method: "POST" });
    const body = await resp.json().catch(() => ({}));
    if (resp.ok) message.success(`已派发重跑：${body.type}`);
    else message.error(`重跑失败（HTTP ${resp.status}）`);
  }

  const columns: ColumnsType<QueueItem> = [
    { title: "时间", dataIndex: "created_at", key: "created_at", width: 160, render: (v: string | null) => v ?? "—" },
    { title: "任务类型", dataIndex: "task_type", key: "task_type", render: (v: string | null) => v ?? "（登记过期）" },
    {
      title: "候选人",
      key: "candidate",
      render: (_: unknown, r: QueueItem) =>
        `${r.candidate_name ?? "—"}${r.job_candidate_id !== null ? `（jc=${r.job_candidate_id}）` : ""}`,
    },
    { title: "状态", dataIndex: "jc_status", key: "jc_status", render: (v: string | null) => v ?? "—" },
    { title: "注记", dataIndex: "note", key: "note", render: (v: string | null) => v ?? "—" },
    {
      title: "操作",
      key: "action",
      render: (_: unknown, r: QueueItem) => (
        <Space>
          {r.job_candidate_id !== null && (
            <Button size="small" onClick={() => rerun(r.job_candidate_id)}>
              重跑
            </Button>
          )}
          <Button size="small" onClick={() => ack(r.task_id)}>
            忽略
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <span style={{ color: "rgba(0,0,0,0.45)", fontSize: 12 }}>
        {items.length} 项（failed_needs_manual 落账）
      </span>
      <Table rowKey="task_id" size="small" columns={columns} dataSource={items} pagination={{ pageSize: 50, showSizeChanger: false }} />
    </div>
  );
}
