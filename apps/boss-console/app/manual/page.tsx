"use client";

import { useCallback, useEffect, useState } from "react";
import { Button, Empty, Table, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { OutcomeCapsule, fmtTime } from "../status";

/**
 * 人工队列。**结构照猎聘**（`.page talent-page` + 计数行 + 表格）。
 *
 * ★ 这一页是**唯一不完全照搬**的：猎聘那边其实是个半成品——它的「重跑」按钮
 *   因为后端端点从来不存在，2026-10-15 就被删了，页面只剩一个只读表。
 *   而 BOSS 这边队列里是**真有东西**的（手机停在风控关卡前等人、采集失败、
 *   轮次超时），所以保留「已处理」按钮——没它就没法销账。
 */
type Item = {
  id: number;
  task_id: string;
  round_name: string;
  outcome: string;
  note: string;
  created_at: string;
};

export default function ManualQueuePage() {
  const [items, setItems] = useState<Item[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    fetch("/api/hr/manual-queue?limit=50")
      .then((r) => r.json())
      .then((b) => setItems(b.items ?? []))
      .catch(() => setItems([]))
      .finally(() => setLoading(false));
  }, []);

  useEffect(load, [load]);

  async function ack(taskId: string) {
    const r = await fetch(`/api/hr/manual-queue/${taskId}/ack`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ note: "工作台已确认" }),
    });
    if (r.ok) message.success("已注记（acked）");
    else message.error(`注记失败（HTTP ${r.status}）`);
    load();
  }

  const columns: ColumnsType<Item> = [
    {
      title: "时间", dataIndex: "created_at", width: 160,
      render: (v: string) => fmtTime(v),
    },
    {
      // ★ 猎聘这一列是原子任务类型（check_attachment…）。BOSS 没有那套任务队列，
      //   它有的是**轮次名**（main_round / daily_round）——同样是「哪类任务出的事」。
      title: "任务类型", dataIndex: "round_name", key: "round_name",
      render: (v: string) => v || "—",
    },
    {
      title: "候选人", key: "candidate",
      // BOSS 的 TaskLog 没挂到候选人上（它不是按候选人入队的）——照约定显示「—」
      render: () => "—",
    },
    {
      title: "状态", dataIndex: "outcome", width: 170,
      render: (v: string) => <OutcomeCapsule outcome={v} />,
    },
    { title: "注记", dataIndex: "note", render: (v: string) => v || "—" },
    {
      title: "操作", width: 100,
      render: (_: unknown, r: Item) => (
        <Button size="small" onClick={() => void ack(r.task_id)}>
          已处理
        </Button>
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
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          最常见的是「手机停在滑块/短信/人脸认证页上」——那三关必须你本人在手机上过。
        </Typography.Text>
      </div>

      <Table
        rowKey="id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={items}
        locale={{ emptyText: <Empty description="没有待处理的事" /> }}
        pagination={{ pageSize: 50, showSizeChanger: false }}
      />
    </div>
  );
}
