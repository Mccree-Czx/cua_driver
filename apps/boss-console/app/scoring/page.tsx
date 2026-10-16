"use client";

import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Card, Space, Tag, Typography, message } from "antd";
import { SaveOutlined } from "@ant-design/icons";

/**
 * 评分偏好。
 *
 * ★ **这一页现在还没有消费者**——BOSS 的打分链路没做（用户已定「先不做打分」）。
 *   建它是因为用户明确要**保留猎聘那个形状**，而且真接上打分时它就是输入，
 *   不用再改表（`jobs.scoring_prefs` 那一列已经在了）。
 *
 * ★ 猎聘上这一页是**孤儿页**（`app-shell.tsx` 的 NAV 里没挂它，只能手敲 URL）。
 *   BOSS 这边刻意**挂进了导航**——一个只能靠记 URL 才找得到的页面，
 *   等于没有。
 */
type Row = {
  job_id: number;
  title: string;
  state: string;
  status: string;
  jd_complete: boolean;
  scoring_prefs: Record<string, unknown>;
};

type Prefs = {
  min_stars: number;
  boosters: { word: string; stars: number }[];
  veto: string[];
  requirements: string[];
};

const EMPTY: Prefs = { min_stars: 3, boosters: [], veto: [], requirements: [] };

export default function ScoringPage() {
  const [rows, setRows] = useState<Row[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await fetch("/api/hr/scoring");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const items: Row[] = (await r.json()).items;
      setRows(items);
      setDraft(Object.fromEntries(
        items.map((j) => [j.job_id, JSON.stringify({ ...EMPTY, ...(j.scoring_prefs ?? {}) }, null, 2)]),
      ));
      setErr(null);
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function save(jobId: number) {
    setBusy(jobId);
    try {
      let parsed: unknown;
      try {
        parsed = JSON.parse(draft[jobId] ?? "{}");
      } catch {
        throw new Error("不是合法 JSON");
      }
      const r = await fetch(`/api/hr/scoring/${jobId}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ scoring_prefs: parsed }),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${r.status}`);
      }
      message.success("已保存");
      await load();
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <h2 className="page-title">评分偏好</h2>

      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 16 }}
        message="这一页现在还没有消费者"
        description="BOSS 的打分链路还没做，所以这里的设置暂时不会影响任何行为。保留它是因为接上打分时它就是输入。"
      />

      {err && <Alert type="error" showIcon message={err} style={{ marginBottom: 16 }} />}

      {rows.map((job) => (
        <Card
          key={job.job_id}
          style={{ marginBottom: 16 }}
          title={
            <Space>
              {job.title}
              <Tag color={job.state === "开放中" ? "green" : "gold"}>{job.state || "开放中"}</Tag>
              {job.jd_complete ? <Tag color="blue">JD 已读全</Tag> : <Tag color="orange">JD 未读全</Tag>}
            </Space>
          }
          extra={
            <Button
              type="primary"
              size="small"
              icon={<SaveOutlined />}
              loading={busy === job.job_id}
              onClick={() => void save(job.job_id)}
            >
              保存
            </Button>
          }
        >
          <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
            形状照猎聘：<code>min_stars</code>（1-5 星最低主动沟通星级）、
            <code>boosters</code>（加分词）、<code>veto</code>（一票否决词）、
            <code>requirements</code>（必要条件）。
          </Typography.Paragraph>
          <textarea
            className="jd-pre"
            style={{ width: "100%", minHeight: 180, fontFamily: "monospace" }}
            value={draft[job.job_id] ?? ""}
            onChange={(e) => setDraft({ ...draft, [job.job_id]: e.target.value })}
          />
        </Card>
      ))}

      {rows.length === 0 && !err && (
        <Alert type="info" showIcon message="还没有职位" description="职位采进来之后这里会列出来。" />
      )}
    </>
  );
}
