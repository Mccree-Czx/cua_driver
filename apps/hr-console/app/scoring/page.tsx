"use client";

import { useEffect, useMemo, useState } from "react";
import { Card, Form, Rate, Input, Button, Select, Space, message } from "antd";

type ScoringPrefs = {
  min_stars?: number;
  boosters?: string[];
  veto?: string[];
  requirements?: string[];
};

type JobItem = { id: number; title: string; scoring_prefs: ScoringPrefs | null };

export default function Scoring() {
  const [jobs, setJobs] = useState<JobItem[]>([]);
  const [jobId, setJobId] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();

  useEffect(() => {
    fetch("/api/jobs")
      .then((r) => r.json())
      .then((b) => {
        const list: JobItem[] = Array.isArray(b) ? b : b.items ?? [];
        setJobs(list);
        if (list.length > 0) setJobId(list[0].id);
      })
      .catch(() => {});
  }, []);

  const currentJob = useMemo(() => jobs.find((j) => j.id === jobId) ?? null, [jobs, jobId]);

  useEffect(() => {
    if (currentJob) {
      const prefs = currentJob.scoring_prefs || {};
      form.setFieldsValue({
        min_stars: prefs.min_stars ?? 3,
        boosters: (prefs.boosters || []).join("\n"),
        veto: (prefs.veto || []).join("\n"),
        requirements: (prefs.requirements || []).join("\n"),
      });
    }
  }, [currentJob, form]);

  async function onSave(values: {
    min_stars: number;
    boosters?: string;
    veto?: string;
    requirements?: string;
  }) {
    if (jobId === null) return;
    const splitLines = (s?: string) =>
      (s || "")
        .split("\n")
        .map((x) => x.trim())
        .filter(Boolean);
    const scoring_prefs = {
      min_stars: values.min_stars,
      boosters: splitLines(values.boosters),
      veto: splitLines(values.veto),
      requirements: splitLines(values.requirements),
    };
    setSaving(true);
    const resp = await fetch(`/api/hr/jobs/${jobId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scoring_prefs }),
    });
    setSaving(false);
    if (resp.ok) message.success("评分偏好已保存");
    else message.error(`保存失败（HTTP ${resp.status}）`);
  }

  return (
    <Card title="评分偏好" style={{ maxWidth: 760 }}>
      <Space direction="vertical" style={{ width: "100%" }} size="large">
        <div>
          <div style={{ color: "rgba(0,0,0,0.45)", fontSize: 13, marginBottom: 8 }}>
            评分偏好按岗位独立配置，选择岗位后编辑该岗位的评分卡。
          </div>
          <Select
            style={{ width: 360 }}
            value={jobId}
            onChange={setJobId}
            options={jobs.map((j) => ({ value: j.id, label: `#${j.id} ${j.title}` }))}
            placeholder="选择岗位"
          />
        </div>

        <div style={{ color: "rgba(0,0,0,0.45)", fontSize: 13 }}>
          评分卡是可选偏好：LLM 结合 JD 与以下偏好给候选人评 1–5 星（3 星基本符合、4
          星完全符合、5 星符合且有亮点）；低于最低沟通星级则不主动触达。
        </div>

        <Form form={form} layout="vertical" onFinish={onSave}>
          <Form.Item name="min_stars" label="最低主动沟通星级">
            <Rate />
          </Form.Item>
          <Form.Item name="boosters" label="加分点（每行一项）">
            <Input.TextArea rows={3} placeholder="符合则提升评级，如：海外销售经验、英语流利" />
          </Form.Item>
          <Form.Item name="veto" label="一票否决点（每行一项）">
            <Input.TextArea rows={3} placeholder="命中任一直接 1 星，如：无 ToB 经验" />
          </Form.Item>
          <Form.Item name="requirements" label="其他要求（每行一项）">
            <Input.TextArea rows={3} placeholder="如：可接受出差" />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={saving} disabled={jobId === null}>
            保存评分偏好
          </Button>
        </Form>
      </Space>
    </Card>
  );
}
