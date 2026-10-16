"use client";

import { useEffect, useState } from "react";
import { Alert, Card, Col, Row, Spin, Statistic, Table } from "antd";
import Link from "next/link";

type Overview = {
  jobs: { total: number; usable: number; 开放中: number; jd_ready: number };
  candidates: number;
  jc: { total: number; greeted: number; resume_received: number; closed: number };
  conversations: { total: number; unread: number };
  messages: { total: number; in: number; out: number };
  greeted: number;
  today: { quota_used: number; quota_cap: number };
};

type Daily = {
  days: { date: string; messages_in: number; messages_out: number; greeted: number }[];
  totals: { messages_in: number; messages_out: number; greeted: number };
};

export default function DashboardPage() {
  const [data, setData] = useState<Overview | null>(null);
  const [daily, setDaily] = useState<Daily | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([
      fetch("/api/hr/overview").then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))),
      fetch("/api/hr/daily?days=14").then((r) => (r.ok ? r.json() : null)),
    ])
      .then(([o, d]) => {
        setData(o);
        setDaily(d);
      })
      .catch((e) => setErr(String(e)));
  }, []);

  if (err) {
    return (
      <Alert
        type="error"
        showIcon
        message="读不到数据"
        description={`${err}。确认 boss-pipeline 起着（uvicorn app.main:app --port 8100）。`}
      />
    );
  }
  if (!data) return <Spin />;

  return (
    <>
      <h2 className="page-title">驾驶舱</h2>

      <Row gutter={[16, 16]}>
        <Col xs={12} md={6}>
          <Card>
            {/* ★ 「可用」比「开放中」严：还要系统启用过 + JD 读全了。
                拿一个没有 JD 的职位去给真实候选人发消息是纯粹的浪费。 */}
            <Statistic title="可用职位" value={data.jobs.usable} />
            <div className="stat-hint">
              开放中 {data.jobs.开放中} · 共 {data.jobs.total} 个 · <Link href="/jobs">看全部</Link>
            </div>
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title="候选人" value={data.candidates} />
            <div className="stat-hint">
              职位下共 {data.jc.total} 条记录 · <Link href="/candidates">看全部</Link>
            </div>
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title="会话" value={data.conversations.total} />
            <div className="stat-hint">
              未读 {data.conversations.unread} · <Link href="/conversations">看会话</Link>
            </div>
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title="今日打招呼" value={data.today.quota_used} suffix={`/ ${data.today.quota_cap}`} />
            <div className="stat-hint">配额用满后轮次会自己停</div>
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} md={12}>
          <Card title="候选人漏斗">
            <Row gutter={16}>
              <Col span={8}>
                <Statistic title="已打招呼" value={data.jc.greeted} />
              </Col>
              <Col span={8}>
                <Statistic title="已收到简历" value={data.jc.resume_received} />
              </Col>
              <Col span={8}>
                <Statistic title="已关闭" value={data.jc.closed} />
              </Col>
            </Row>
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card title="消息">
            <Row gutter={16}>
              <Col span={8}>
                <Statistic title="收到" value={data.messages.in} />
              </Col>
              <Col span={8}>
                <Statistic title="发出" value={data.messages.out} />
              </Col>
              <Col span={8}>
                <Statistic title="累计打招呼" value={data.greeted} />
              </Col>
            </Row>
            <div className="stat-hint" style={{ marginTop: 12 }}>
              ★ 发出数只统计**我们单位发出去的**。BOSS 上点「立即沟通」会自动发一条
              账号预设的招呼语，那条也算在内。
            </div>
          </Card>
        </Col>
      </Row>

      <Card title="近 14 天" style={{ marginTop: 16 }}>
        <Table
          size="small"
          rowKey="date"
          pagination={false}
          dataSource={daily?.days ?? []}
          columns={[
            { title: "日期", dataIndex: "date", width: 140 },
            {
              title: "收到消息", dataIndex: "messages_in", width: 120,
              sorter: (a, b) => a.messages_in - b.messages_in,
            },
            { title: "发出消息", dataIndex: "messages_out", width: 120 },
            { title: "打招呼", dataIndex: "greeted", width: 120 },
          ]}
        />
      </Card>
    </>
  );
}
