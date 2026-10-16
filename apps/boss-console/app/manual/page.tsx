"use client";

import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Empty, Input, Space, Table, Tag, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";

/**
 * 人工队列。
 *
 * ★ **BOSS 上这一页有真内容**（猎聘那边其实是半成品——它的「重跑」端点
 *   根本不存在，按钮早删了）。BOSS 落在这里的是：
 *
 * - **手机停在风控关卡前等人**（滑块 / 短信 / 人脸）——这是最常见的
 * - 采集失败（职位/候选人/会话抓一半）
 * - 轮次超时
 *
 * 处置是 `ack`：**不删行**，把 outcome 改成 acked 并追加备注，留痕。
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
  const [rows, setRows] = useState<Item[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch("/api/hr/manual-queue");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setRows((await r.json()).items);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function ack(taskId: string) {
    setBusy(taskId);
    try {
      const r = await fetch(`/api/hr/manual-queue/${taskId}/ack`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ note }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      message.success("已确认");
      setNote("");
      await load();
    } catch (e) {
      message.error(String(e));
    } finally {
      setBusy(null);
    }
  }

  const columns: ColumnsType<Item> = [
    { title: "轮次", dataIndex: "round_name", width: 140 },
    {
      title: "说明", dataIndex: "note",
      render: (n: string) => <Typography.Text>{n || "（没写原因）"}</Typography.Text>,
    },
    { title: "task_id", dataIndex: "task_id", width: 220, ellipsis: true },
    {
      title: "时间", dataIndex: "created_at", width: 170,
      render: (t: string) => t.slice(0, 16).replace("T", " "),
    },
    {
      title: "操作", width: 110,
      render: (_: unknown, r: Item) => (
        <Button size="small" loading={busy === r.task_id} onClick={() => void ack(r.task_id)}>
          已处理
        </Button>
      ),
    },
  ];

  return (
    <>
      <h2 className="page-title">人工队列</h2>

      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="这里放的是机器做不了的事"
        description="最常见的是「手机停在滑块/短信/人脸认证页上」——那三关必须你本人在手机上过。过完之后回到这里点「已处理」。"
      />

      <Space style={{ marginBottom: 16 }} wrap>
        <Input
          placeholder="处理备注（留下干嘛了，方便回溯）"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          style={{ width: 360 }}
        />
        <Button onClick={() => void load()}>刷新</Button>
      </Space>

      {err && <Alert type="error" showIcon message={err} style={{ marginBottom: 16 }} />}
      {!loading && rows.length === 0 && !err && (
        <Empty description="没有待处理的事 —— 挺好" />
      )}

      {rows.length > 0 && (
        <Table<Item>
          rowKey="id"
          size="middle"
          loading={loading}
          columns={columns}
          dataSource={rows}
          pagination={{ pageSize: 20, showSizeChanger: false }}
        />
      )}
    </>
  );
}
