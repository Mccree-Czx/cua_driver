"use client";

import { useEffect, useState } from "react";
import {
  Alert, Button, Card, Col, Collapse, Form, Input, Rate, Row, Spin, Statistic, Table,
  Typography, message,
} from "antd";
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

// ——— 评分偏好（驾驶舱里每个「可用职位」一张卡）———

type ScoringPrefs = {
  min_stars?: number;
  boosters?: string[];
  veto?: string[];
  requirements?: string[];
};

type ScorableJob = {
  job_id: number;
  title: string;
  jd_text: string | null;
  scoring_prefs: ScoringPrefs;
};

/**
 * 照猎聘 `app/page.tsx` 的 `JobScoringForm`。
 *
 * ★ **它不是独立一栏**——用户指出猎聘的「评分偏好」就长在职位上，
 *   而我一开始把它做成了导航里单独一项。现在并进驾驶舱的职位卡。
 *
 * ★ 这一版比猎聘多一样：**把 JD 一起显示出来**。
 *   打分偏好脱离了 JD 看没有意义（「加分点」是相对什么加分？），
 *   猎聘那边 JD 也在同一张卡上，只是它的 `/api/hr/overview` 顺带带了。
 */
function JobScoringForm({ job, onSaved }: { job: ScorableJob; onSaved: () => void }) {
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const p = job.scoring_prefs || {};
    form.setFieldsValue({
      min_stars: p.min_stars ?? 3,
      boosters: (p.boosters || []).join("\n"),
      veto: (p.veto || []).join("\n"),
      requirements: (p.requirements || []).join("\n"),
    });
  }, [job, form]);

  async function onSave(values: {
    min_stars: number; boosters?: string; veto?: string; requirements?: string;
  }) {
    const lines = (s?: string) => (s || "").split("\n").map((x) => x.trim()).filter(Boolean);
    setSaving(true);
    try {
      const r = await fetch(`/api/hr/scoring/${job.job_id}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          scoring_prefs: {
            min_stars: values.min_stars,
            boosters: lines(values.boosters),
            veto: lines(values.veto),
            requirements: lines(values.requirements),
          },
        }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      message.success(`${job.title} 已保存`);
      onSaved();
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Form form={form} layout="vertical" onFinish={onSave}>
      <div style={{ color: "var(--text-secondary)", fontSize: 12, fontWeight: 600, marginBottom: 8 }}>
        职位描述 / JD
      </div>
      <pre className="jd-pre" style={{ marginBottom: 16 }}>
        {job.jd_text || "（暂无 JD）"}
      </pre>
      <Form.Item name="min_stars" label="最低主动沟通星级">
        <Rate />
      </Form.Item>
      <Form.Item name="boosters" label="加分点（每行一项）">
        <Input.TextArea rows={2} placeholder="符合则提升评级，如：有实时数仓经验" />
      </Form.Item>
      <Form.Item name="veto" label="一票否决点（每行一项）">
        <Input.TextArea rows={2} placeholder="命中任一直接 1 星，如：无数据开发经验" />
      </Form.Item>
      <Form.Item name="requirements" label="其他要求（每行一项）">
        <Input.TextArea rows={2} placeholder="如：可接受出差" />
      </Form.Item>
      <Button type="primary" htmlType="submit" loading={saving}>
        保存评分偏好
      </Button>
    </Form>
  );
}

export default function DashboardPage() {
  const [data, setData] = useState<Overview | null>(null);
  const [daily, setDaily] = useState<Daily | null>(null);
  const [scorable, setScorable] = useState<ScorableJob[]>([]);
  const [err, setErr] = useState<string | null>(null);

  function loadScorable() {
    return fetch("/api/hr/scoring?usable_only=true")
      .then((r) => (r.ok ? r.json() : { items: [] }))
      .then((d) => setScorable(d.items ?? []))
      .catch(() => setScorable([]));
  }

  useEffect(() => {
    Promise.all([
      fetch("/api/hr/overview").then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))),
      fetch("/api/hr/daily?days=14").then((r) => (r.ok ? r.json() : null)),
      loadScorable(),
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

      {/* ★ 评分偏好长在职位上，不是导航里单独一栏——照猎聘。
          只列「可用职位」（系统启用 + BOSS 在招 + JD 读全），判定在服务端做。 */}
      <Card title="可用职位的评分偏好" style={{ marginTop: 16 }}>
        {scorable.length === 0 ? (
          <Typography.Text type="secondary">
            还没有可用职位（要 系统启用 + BOSS 在招 + JD 读全）。
            没有 JD 的职位不参与，因为拿它去筛人是浪费。
          </Typography.Text>
        ) : (
          <Collapse
            items={scorable.map((j) => ({
              key: String(j.job_id),
              label: j.title,
              children: <JobScoringForm job={j} onSaved={() => void loadScorable()} />,
            }))}
          />
        )}
      </Card>

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
