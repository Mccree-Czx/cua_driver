"use client";

import { use, useEffect, useState } from "react";
import { Alert, Card, Descriptions, Empty, Spin, Table, Tag, Typography } from "antd";
import Link from "next/link";
import { StatusCapsule } from "../../status";

/**
 * 职位详情页。**BOSS 特有**——猎聘没有这一页（它的 JD 在抽屉里展开）。
 *
 * 单独开一页是因为 **BOSS 的 JD 很长**（实测 300~1100 字，还带【岗位职责】
 * 【任职要求】这种分段），塞进抽屉里读着累。
 *
 * 页面上顺带列出这个职位下的候选人（jc 维度）。
 */
type JobDetail = {
  id: number;
  title: string;
  state: string;
  status: string;
  details: string;
  jd_text: string | null;
  jd_complete: boolean;
  tags: string[];
  jd_chars: number;
  scoring_prefs: Record<string, unknown>;
  candidates: { jc_id: number; candidate_id: number; status: string; name: string }[];
};

export default function JobDetailPage({ params }: { params: Promise<{ id: string }> }) {
  // Next 16 的 params 是 Promise——用 use() 解（跟 app-shell 里 use(io()) 一个路子）
  const { id } = use(params);
  const [job, setJob] = useState<JobDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    fetch(`/api/jobs/${id}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setJob)
      .catch((e) => setErr(String(e)));
  }, [id]);

  if (err) return <Alert type="error" showIcon message={err} />;
  if (!job) return <Spin />;

  return (
    <>
      <h2 className="page-title">
        <Link href="/jobs">职位</Link> <Typography.Text type="secondary">/ {job.title}</Typography.Text>
      </h2>

      <Card style={{ marginBottom: 16 }}>
        <Descriptions column={{ xs: 1, md: 2 }} size="small">
          <Descriptions.Item label="BOSS 状态">
            <Tag color={job.state === "开放中" ? "green" : job.state === "已关闭" ? "default" : "gold"}>
              {job.state || "开放中"}
            </Tag>
          </Descriptions.Item>
          <Descriptions.Item label="系统启用">
            <Tag color={job.status === "active" ? "green" : "default"}>
              {job.status === "active" ? "启用" : "停用"}
            </Tag>
          </Descriptions.Item>
          <Descriptions.Item label="要求" span={2}>{job.details || "—"}</Descriptions.Item>
          <Descriptions.Item label="JD" span={2}>
            {job.jd_complete ? (
              <Tag color="blue">{job.jd_chars} 字 · 已读全</Tag>
            ) : (
              <Tag color="orange">{job.jd_chars} 字 · 未读全</Tag>
            )}
          </Descriptions.Item>
          <Descriptions.Item label="标签" span={2}>
            {(job.tags ?? []).map((t) => <Tag key={t}>{t}</Tag>)}
          </Descriptions.Item>
        </Descriptions>
        <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0, fontSize: 12 }}>
          ★ 职位名、城市、招聘类型在 BOSS 上**发布后不可修改**（页面原文），
          所以这里也改不了——想改只能下架重发。
        </Typography.Paragraph>
      </Card>

      <Card title="职位描述" style={{ marginBottom: 16 }}>
        {job.jd_text ? (
          <pre className="jd-pre">{job.jd_text}</pre>
        ) : (
          <Empty description="还没抓到 JD。没有 JD 的职位不会被轮次使用。" />
        )}
      </Card>

      <Card title={`候选人（${job.candidates?.length ?? 0}）`}>
        <Table
          size="small"
          rowKey="jc_id"
          pagination={false}
          dataSource={job.candidates ?? []}
          columns={[
            {
              title: "姓名", dataIndex: "name", width: 160,
              render: (n: string, r) => <Link href={`/candidates?focus=${r.jc_id}`}>{n}</Link>,
            },
            {
              title: "状态", dataIndex: "status", width: 140,
              render: (s: string) => <StatusCapsule status={s} />,
            },
          ]}
        />
      </Card>
    </>
  );
}
