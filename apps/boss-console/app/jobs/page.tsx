"use client";

import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Input, Segmented, Space, Spin, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";

type Job = {
  id: number;
  title: string;
  state: string;
  details: string;
  jd_complete: boolean;
  tags: string[];
  jd_chars: number;
  jd_text?: string;
};

/** 状态色。★ 直接拿 BOSS 给的中文当键——服务端就是原样存的，不做映射（见 models.py）。 */
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

  const columns: ColumnsType<Job> = [
    { title: "职位", dataIndex: "title", width: 240 },
    {
      title: "状态",
      dataIndex: "state",
      width: 110,
      // 「开放中」在服务端可能就是空串（BOSS 对在线职位不打标签），那也当开放中
      render: (s: string) => <Tag color={STATE_COLOR[s] ?? "green"}>{s || "开放中"}</Tag>,
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
          // ★ 半截 JD 要显眼地标出来：它不能用来打分，但也不能假装没有
          <Tag color="orange">{row.jd_chars} 字 · 未读全</Tag>
        ),
    },
    {
      title: "标签",
      dataIndex: "tags",
      width: 260,
      render: (tags: string[]) => (tags ?? []).slice(0, 3).map((t) => <Tag key={t}>{t}</Tag>),
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
        <Input.Search placeholder="搜职位名" allowClear style={{ width: 240 }}
                      onSearch={setQ} />
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
        expandable={{
          // 展开才看 JD——它可能上千字，塞进表格会把行撑爆
          expandedRowRender: (row) => (
            <pre className="jd-pre">{row.jd_text ?? "（还没抓到 JD）"}</pre>
          ),
          rowExpandable: (row) => row.jd_complete,
        }}
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
