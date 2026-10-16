"use client";

import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Card, Drawer, Empty, Input, Space, Table, Tag, message } from "antd";
import type { ColumnsType } from "antd/es/table";

/**
 * 职位。**结构照猎聘的 `hr-console/app/jobs/page.tsx`**。
 *
 * ★★ 本次补上的是一个**真的功能缺口**：猎聘有「编辑职位」抽屉（补 JD + 一键启用），
 *    而 BOSS 的后端 `PATCH /api/hr/jobs/{job_id}` **早就支持改 `jd_text` 和 `status`**，
 *    前端却一直没暴露——所以之前只能切启停、**补不了 JD**。
 *
 * ★ 为什么 JD 这么要紧：它是筛选候选人的唯一依据。没有它，系统只能凭空判断
 *   合不合适，然后给**真实的人**发消息。所以 `MIN_JD_CHARS` 是道闸门：
 *   不到字数不许启用（照猎聘）。
 */

/** 少于这个字数不许启用。照猎聘（`hr-console/app/jobs/page.tsx:32`）。 */
const MIN_JD_CHARS = 30;

type Job = {
  id: number;
  title: string;
  state: string;        // BOSS 原生：开放中 / 待开放 / 已关闭 / 审核未通过
  status: string;       // 系统侧：active / draft
  details: string;
  jd_complete: boolean;
  tags: string[];
  jd_chars: number;
};

/** 系统侧状态 → 展示。两列（BOSS 的 `state` + 系统 `status`）合成一列，照猎聘。 */
function jobStatusMeta(job: Job): { label: string; color: string } {
  if (job.status === "active" && job.jd_complete) return { label: "在招", color: "green" };
  if (!job.jd_complete) return { label: "待补 JD", color: "orange" };
  return { label: "已停用", color: "default" };
}

export default function JobsPage() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<Job | null>(null);
  const [jd, setJd] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    fetch("/api/jobs")
      .then((r) => r.json())
      .then((b) => setJobs(b.items ?? []))
      .catch(() => setJobs([]))
      .finally(() => setLoading(false));
  }, []);

  useEffect(load, [load]);

  async function openEditor(job: Job) {
    setEditing(job);
    // 列表接口不带 JD 正文（可能上千字），进抽屉时单独取一次
    try {
      const r = await fetch(`/api/jobs/${job.id}`);
      const d = await r.json();
      setJd(d.jd_text ?? "");
    } catch {
      setJd("");
    }
  }

  async function save(enable?: "active") {
    if (!editing) return;
    setSaving(true);
    try {
      const body: Record<string, unknown> = { jd_text: jd };
      if (enable) body.status = enable;
      const r = await fetch(`/api/hr/jobs/${editing.id}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${r.status}`);
      }
      message.success(enable ? "已保存并启用" : "已保存");
      setEditing(null);
      load();
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  const jdReady = jd.trim().length >= MIN_JD_CHARS;

  const columns: ColumnsType<Job> = [
    {
      title: "职位",
      dataIndex: "title",
      render: (title: string, job) => (
        <Space direction="vertical" size={2}>
          <b>{title}</b>
          {/* BOSS 的 `details` 是一整串「上海·四川北路 ｜ 本科 ｜ 3-5年 ｜ 18-25K·13薪 ｜ 全职」，
              猎聘是四个独立字段——这里原样显示，**不拆**（拆了 BOSS 改格式就碎）。 */}
          <span style={{ color: "rgba(0,0,0,0.45)", fontSize: 12 }}>
            {job.details || "（无详情）"}
          </span>
        </Space>
      ),
    },
    {
      title: "状态",
      width: 110,
      render: (_: unknown, job) => {
        const meta = jobStatusMeta(job);
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    {
      title: "JD",
      width: 110,
      render: (_: unknown, job) =>
        job.jd_complete && job.jd_chars >= MIN_JD_CHARS ? (
          <span style={{ color: "rgba(0,0,0,0.45)" }}>{job.jd_chars} 字</span>
        ) : (
          <Tag color="red">缺失</Tag>
        ),
    },
    {
      title: "",
      width: 130,
      render: (_: unknown, job) => (
        <Button
          size="small"
          type={job.jd_complete ? "default" : "primary"}
          onClick={() => void openEditor(job)}
        >
          {job.jd_complete ? "编辑" : "补 JD 并启用"}
        </Button>
      ),
    },
  ];

  const pending = jobs.filter((j) => !j.jd_complete).length;

  return (
    <Card title="职位">
      {pending > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={`有 ${pending} 个职位还没补 JD，系统不会拿它们去招人`}
          description="扫回来的职位只有名字和地点学历薪资。JD 是筛选候选人的唯一依据——没有它，系统只能凭空判断合不合适，然后给真实的人发消息。补上并启用后，下一轮就会用它们。"
        />
      )}

      <Table
        rowKey="id"
        columns={columns}
        dataSource={jobs}
        loading={loading}
        pagination={false}
        locale={{ emptyText: <Empty description="还没有职位（采数端扫回来之后会出现在这里）" /> }}
      />

      <Drawer
        width={620}
        open={editing !== null}
        onClose={() => setEditing(null)}
        title={editing ? `编辑职位 · ${editing.title}` : ""}
        extra={
          <Space>
            <Button onClick={() => void save()} loading={saving}>
              仅保存
            </Button>
            <Button
              type="primary"
              disabled={!jdReady}
              loading={saving}
              onClick={() => void save("active")}
            >
              保存并启用
            </Button>
          </Space>
        }
      >
        <Space direction="vertical" size="large" style={{ width: "100%" }}>
          <div style={{ color: "rgba(0,0,0,0.45)", fontSize: 13 }}>
            JD 是系统判断候选人合不合适的唯一依据。少于 {MIN_JD_CHARS} 字不允许启用——
            那时候系统只能靠猜，而猜错的代价是给真实的人发错消息。
          </div>

          <div>
            <div style={{ marginBottom: 8 }}>职位描述（JD）</div>
            <Input.TextArea
              rows={16}
              value={jd}
              onChange={(e) => setJd(e.target.value)}
              placeholder="把该职位的职责与要求粘在这里"
            />
            <div
              style={{
                marginTop: 4,
                fontSize: 12,
                color: jdReady ? "rgba(0,0,0,0.45)" : "#cf1322",
              }}
            >
              {jd.trim().length} / {MIN_JD_CHARS} 字
            </div>
          </div>

          <Alert
            type="info"
            showIcon
            message="筛选严格度在「驾驶舱 → 在招职位」里调"
            description="系统按星级（1–5）判断候选人合不合适。这个页只管 JD 与启停。"
          />
        </Space>
      </Drawer>
    </Card>
  );
}
