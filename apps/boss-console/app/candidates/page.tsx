"use client";

import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Card, Col, Empty, Input, Row, Space, Spin, Tag, Typography } from "antd";

type Candidate = {
  id: number;
  name: string;
  job_and_company: string;
  work_status: string;
  work_year: string;
  degree: string;
  age: string;
  description: string | null;
  summary: string;
  expect_job_city: string;
  expect_salary: string;
  expect_industry: string;
  tags: string[];
  company_history: string[];
  active: string;
};

export default function CandidatesPage() {
  const [rows, setRows] = useState<Candidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const p = new URLSearchParams();
      if (q.trim()) p.set("q", q.trim());
      const r = await fetch(`/api/candidates?${p}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setRows((await r.json()).items);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, [q]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <>
      <h2 className="page-title">候选人</h2>

      <Space style={{ marginBottom: 16 }} wrap>
        <Input.Search placeholder="搜姓名 / 公司" allowClear style={{ width: 280 }}
                      onSearch={setQ} />
        <Button onClick={() => void load()}>刷新</Button>
      </Space>

      {err && <Alert type="error" showIcon message={err} style={{ marginBottom: 16 }} />}
      {loading && <Spin />}
      {!loading && rows.length === 0 && !err && (
        <Empty description="还没有候选人数据" />
      )}

      <Row gutter={[16, 16]}>
        {rows.map((c) => (
          <Col xs={24} md={12} xl={8} key={c.id}>
            <Card
              size="small"
              title={
                <Space>
                  <b>{c.name}</b>
                  {/* 列表页采到的概要；详情页采到更多 */}
                  <Typography.Text type="secondary">{c.summary}</Typography.Text>
                </Space>
              }
              extra={c.active ? <Tag>{c.active}</Tag> : null}
            >
              <div className="cand-line">{c.job_and_company || "（未知公司·职位）"}</div>
              <div className="cand-line">
                {[c.work_year, c.degree, c.age, c.work_status].filter(Boolean).join(" · ")}
              </div>
              <div className="cand-line">
                期望：{c.expect_job_city || "—"} · {c.expect_salary || "—"}
                {c.expect_industry && c.expect_industry !== "行业不限" ? ` · ${c.expect_industry}` : ""}
              </div>
              {c.description && (
                <Typography.Paragraph
                  type="secondary"
                  ellipsis={{ rows: 3, expandable: true, symbol: "展开" }}
                  style={{ marginTop: 8, marginBottom: 0 }}
                >
                  {c.description}
                </Typography.Paragraph>
              )}
              <div style={{ marginTop: 8 }}>
                {(c.tags ?? []).map((t) => (
                  <Tag key={t} style={{ marginBottom: 4 }}>{t}</Tag>
                ))}
              </div>
            </Card>
          </Col>
        ))}
      </Row>
    </>
  );
}
