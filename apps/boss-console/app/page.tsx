"use client";

import { useEffect, useState } from "react";
import { Alert, Card, Col, Row, Spin, Statistic } from "antd";
import Link from "next/link";

type Overview = {
  jobs: { total: number; 开放中: number; 待开放: number; jd_ready: number };
  candidates: number;
  conversations: { total: number; unread: number };
  messages: { total: number; in: number; out: number };
  greeted: number;
};

export default function DashboardPage() {
  const [data, setData] = useState<Overview | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/overview")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setData)
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
            <Statistic title="在线职位" value={data.jobs["开放中"]} />
            <div className="stat-hint">
              共 {data.jobs.total} 个职位 · <Link href="/jobs">看全部</Link>
            </div>
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title="可打分职位" value={data.jobs.jd_ready} />
            {/* ★ 这个数比「职位总数」重要：半截 JD 打出来的分是错的，
                所以只有确认读全的 JD 才算数（见 services/boss-pipeline/app/models.py）。 */}
            <div className="stat-hint">JD 已读全的才有这个数</div>
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title="候选人" value={data.candidates} />
            <div className="stat-hint">
              <Link href="/candidates">看全部</Link>
            </div>
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title="已打招呼" value={data.greeted} />
            <div className="stat-hint">本系统发出去的</div>
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} md={12}>
          <Card title="会话">
            <Row gutter={16}>
              <Col span={8}>
                <Statistic title="会话数" value={data.conversations.total} />
              </Col>
              <Col span={8}>
                <Statistic title="未读" value={data.conversations.unread} />
              </Col>
              <Col span={8}>
                <Statistic title="消息" value={data.messages.total} />
              </Col>
            </Row>
            <div className="stat-hint" style={{ marginTop: 12 }}>
              收到 {data.messages.in} · 发出 {data.messages.out}　<Link href="/conversations">看会话</Link>
            </div>
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card title="说明">
            <p className="stat-hint" style={{ lineHeight: 1.9 }}>
              数据来自 Mac 上 <code>agent_boss</code> 的只读流程
              （扫职位 / 抓 JD / 看候选人 / 读会话），经 <code>/api/ingest/*</code> 推进来。
              <br />
              <b>打招呼</b>是唯一会写出去的动作，默认干跑、且本地与服务端各存一份账本防重复。
            </p>
          </Card>
        </Col>
      </Row>
    </>
  );
}
