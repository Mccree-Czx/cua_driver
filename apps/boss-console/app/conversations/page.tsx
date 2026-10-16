"use client";

import { useCallback, useEffect, useState } from "react";
import { Alert, Badge, Button, Empty, Input, List, Space, Spin, Tag, Typography } from "antd";

type Conversation = {
  id: number;
  boss_name: string;
  job_title: string;
  last_msg: string;
  status: string;
  boss_time: string;
  unread: number;
};

type Message = { id: number; direction: string; text: string; first_seen_at: string };

export default function ConversationsPage() {
  const [rows, setRows] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [openId, setOpenId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const p = new URLSearchParams();
      if (q.trim()) p.set("q", q.trim());
      if (unreadOnly) p.set("unread_only", "true");
      const r = await fetch(`/api/hr/conversations?${p}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setRows((await r.json()).items);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, [q, unreadOnly]);

  useEffect(() => {
    void load();
  }, [load]);

  async function open(id: number) {
    setOpenId(id);
    const r = await fetch(`/api/hr/conversations/${id}`);
    setMessages(r.ok ? (await r.json()).messages : []);
  }

  return (
    <>
      <h2 className="page-title">会话</h2>

      <Space style={{ marginBottom: 16 }} wrap>
        <Input.Search placeholder="搜候选人" allowClear style={{ width: 240 }} onSearch={setQ} />
        <Button type={unreadOnly ? "primary" : "default"}
                onClick={() => setUnreadOnly((v) => !v)}>
          只看未读
        </Button>
        <Button onClick={() => void load()}>刷新</Button>
      </Space>

      {err && <Alert type="error" showIcon message={err} style={{ marginBottom: 16 }} />}
      {loading && <Spin />}
      {!loading && rows.length === 0 && !err && <Empty description="还没有会话数据" />}

      <List
        itemLayout="horizontal"
        dataSource={rows}
        renderItem={(c) => (
          <>
            <List.Item
              actions={[
                <Button key="open" size="small" type="link"
                        onClick={() => void open(c.id)}>
                  {openId === c.id ? "收起" : "看消息"}
                </Button>,
              ]}
            >
              <List.Item.Meta
                avatar={
                  <Badge count={c.unread} size="small">
                    <div className="conv-avatar">{c.boss_name.slice(0, 1)}</div>
                  </Badge>
                }
                title={
                  <Space>
                    <b>{c.boss_name}</b>
                    <Typography.Text type="secondary">{c.job_title}</Typography.Text>
                    {c.status && <Tag>{c.status}</Tag>}
                  </Space>
                }
                description={
                  <Typography.Text type="secondary" ellipsis>
                    {c.boss_time}　{c.last_msg}
                  </Typography.Text>
                }
              />
            </List.Item>
            {openId === c.id && (
              <div className="chat-preview">
                {messages.length === 0 && <Typography.Text type="secondary">（没有消息）</Typography.Text>}
                {messages.map((m) => (
                  <div key={m.id} className={`bubble bubble-${m.direction}`}>
                    <div className="bubble-text">{m.text}</div>
                    <div className="bubble-meta">
                      {m.direction === "out" ? "发出" : "收到"} · {m.first_seen_at.slice(0, 16).replace("T", " ")}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      />
    </>
  );
}
