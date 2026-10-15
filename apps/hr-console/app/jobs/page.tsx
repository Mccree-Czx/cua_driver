"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Drawer,
  Empty,
  Input,
  InputNumber,
  Space,
  Table,
  Tag,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";

type Job = {
  id: number;
  title: string;
  jd_text: string;
  location: string | null;
  education: string | null;
  experience: string | null;
  salary: string | null;
  llm_threshold: number;
  status: string;
};

/** 一个职位要能被系统拿去打招呼，JD 至少得有这么多字（后端同一常量把关）。 */
const MIN_JD_CHARS = 30;

const JOB_STATUS: Record<string, { label: string; color: string }> = {
  active: { label: "在招", color: "green" },
  draft: { label: "待补 JD", color: "orange" },
};

/**
 * 职位列表（2026-10-15）。
 *
 * 换到新账号之后，系统会在手机上把该号的在招职位扫回来，**但一律先落成 draft**：
 * 扫到的只是一个名字和地点学历薪资，**没有 JD**。而 JD 是打分的唯一依据——
 * 放一个没有 JD 的职位去 active，等于让 LLM 凭空判断谁合适，然后给真实候选人发招呼。
 *
 * 所以这个页面的核心动作就一个：**把 JD 补上，然后启用**。
 */
export default function Jobs() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<Job | null>(null);
  const [jd, setJd] = useState("");
  const [threshold, setThreshold] = useState(40);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await fetch("/api/jobs");
      const body = await resp.json();
      setJobs(Array.isArray(body) ? body : (body.items ?? []));
    } catch {
      message.error("读取职位列表失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  function openEditor(job: Job) {
    setEditing(job);
    setJd(job.jd_text ?? "");
    setThreshold(job.llm_threshold);
  }

  /** `status` 只在「保存并启用」时传——不传就等于只改 JD，不影响启停。 */
  async function save(nextStatus?: "active" | "draft") {
    if (!editing) return;
    setSaving(true);
    try {
      const resp = await fetch(`/api/hr/jobs/${editing.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          jd_text: jd,
          llm_threshold: threshold,
          ...(nextStatus ? { status: nextStatus } : {}),
        }),
      });
      if (!resp.ok) throw new Error(await detailOf(resp));
      message.success(nextStatus === "active" ? "已启用，下一轮就会用它招聘" : "已保存");
      setEditing(null);
      await load();
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
          <span style={{ color: "rgba(0,0,0,0.45)", fontSize: 12 }}>
            {[job.location, job.education, job.experience, job.salary]
              .filter(Boolean)
              .join(" · ") || "（无详情）"}
          </span>
        </Space>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 110,
      render: (status: string) => {
        const meta = JOB_STATUS[status] ?? { label: status, color: "default" };
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    {
      title: "JD",
      dataIndex: "jd_text",
      width: 110,
      render: (text: string) =>
        (text ?? "").trim().length >= MIN_JD_CHARS ? (
          <span style={{ color: "rgba(0,0,0,0.45)" }}>{(text ?? "").trim().length} 字</span>
        ) : (
          <Tag color="red">缺失</Tag>
        ),
    },
    {
      title: "",
      width: 130,
      render: (_, job) => (
        <Button size="small" type={job.status === "draft" ? "primary" : "default"} onClick={() => openEditor(job)}>
          {job.status === "draft" ? "补 JD 并启用" : "编辑"}
        </Button>
      ),
    },
  ];

  const pending = jobs.filter((j) => j.status !== "active").length;

  return (
    <Card title="职位">
      {pending > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={`有 ${pending} 个职位还没补 JD，系统不会拿它们去打招呼`}
          description="扫回来的职位只有名字和地点学历薪资。JD 是打分的唯一依据——没有它，系统只能凭空判断候选人合不合适，然后给真实候选人发消息。补上并启用后，下一轮就会用它们招聘。"
        />
      )}

      <Table
        rowKey="id"
        columns={columns}
        dataSource={jobs}
        loading={loading}
        pagination={false}
        locale={{ emptyText: <Empty description="这个账号名下还没有职位（切换账号后会自动扫回来）" /> }}
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
            <Button type="primary" disabled={!jdReady} loading={saving} onClick={() => void save("active")}>
              保存并启用
            </Button>
          </Space>
        }
      >
        <Space direction="vertical" size="large" style={{ width: "100%" }}>
          <div style={{ color: "rgba(0,0,0,0.45)", fontSize: 13 }}>
            JD 是系统判断候选人合不合适的唯一依据。少于 {MIN_JD_CHARS} 字不允许启用——
            那时候系统只能靠猜，而猜错的代价是给真实候选人发错消息。
          </div>

          <div>
            <div style={{ marginBottom: 8 }}>职位描述（JD）</div>
            <Input.TextArea
              rows={16}
              value={jd}
              onChange={(e) => setJd(e.target.value)}
              placeholder="把该职位的职责与要求粘在这里"
            />
            <div style={{ marginTop: 4, fontSize: 12, color: jdReady ? "rgba(0,0,0,0.45)" : "#cf1322" }}>
              {jd.trim().length} / {MIN_JD_CHARS} 字
            </div>
          </div>

          <div>
            <div style={{ marginBottom: 8 }}>LLM 阈值（遗留 0–100）</div>
            <InputNumber min={0} max={100} value={threshold} onChange={(v) => setThreshold(v ?? 40)} />
          </div>
        </Space>
      </Drawer>
    </Card>
  );
}

async function detailOf(resp: Response): Promise<string> {
  try {
    const data = await resp.json();
    if (typeof data?.detail === "string") return data.detail;
  } catch {
    // 响应不是 JSON，退回状态码
  }
  return `HTTP ${resp.status}`;
}
