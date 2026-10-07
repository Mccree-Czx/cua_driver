"use client";

import { useCallback, useEffect, useState } from "react";

type CandidateRow = {
  jc_id: number;
  name: string;
  liepin_user_id: string;
  source: string;
  status: string;
  match_score: number | null;
  judge_reason: string | null;
  has_snapshot: boolean;
  has_pdf: boolean;
  last_touch_at: string | null;
};

type Detail = {
  jc: { id: number; status: string; match_score: number | null; judge_reason: string | null };
  candidate: { name: string; liepin_user_id: string; online_resume_minimal: Record<string, string> | null };
  snapshot_url: string | null;
  resume_url: string | null;
  interactions: { direction: string; msg_type: string; content: string | null; sent_at: string | null }[];
};

const STATUS_OPTIONS = [
  "",
  "new",
  "screened_pass",
  "resume_requested",
  "awaiting_resume",
  "resume_received",
  "hr_reviewed",
  "no_response",
  "closed",
  "rejected_hard",
  "rejected_llm",
];

export default function Candidates() {
  const [rows, setRows] = useState<CandidateRow[]>([]);
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [message, setMessage] = useState("");

  const load = useCallback(() => {
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    if (q) params.set("q", q);
    fetch(`/api/hr/candidates?${params}`)
      .then((r) => r.json())
      .then((body) => setRows(body.items ?? []))
      .catch(() => setRows([]));
  }, [status, q]);

  useEffect(load, [load]);

  async function openDetail(jcId: number) {
    setMessage("");
    const resp = await fetch(`/api/hr/candidates/${jcId}`);
    if (resp.ok) setDetail(await resp.json());
  }

  async function review(decision: "approve" | "reject") {
    if (!detail) return;
    const resp = await fetch(`/api/hr/candidates/${detail.jc.id}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, note: decision === "approve" ? "复核确认" : "复核推翻" }),
    });
    const body = await resp.json().catch(() => ({}));
    setMessage(resp.ok ? `复核完成：${body.status}` : `复核失败（HTTP ${resp.status}）`);
    load();
    openDetail(detail.jc.id);
  }

  async function rerun() {
    if (!detail) return;
    const resp = await fetch(`/api/hr/candidates/${detail.jc.id}/rerun`, { method: "POST" });
    const body = await resp.json().catch(() => ({}));
    setMessage(resp.ok ? `已派发重跑：${body.type}` : `重跑失败（HTTP ${resp.status}：${body.detail ?? ""}）`);
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold">候选人</h1>
        <select
          className="rounded border border-gray-300 px-2 py-1 text-sm"
          value={status}
          onChange={(e) => setStatus(e.target.value)}
        >
          {STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s || "全部状态"}
            </option>
          ))}
        </select>
        <input
          className="w-56 rounded border border-gray-300 px-2 py-1 text-sm"
          placeholder="搜姓名 / liepin id"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <span className="text-xs text-gray-500">{rows.length} 行</span>
        {message && <span className="text-xs text-blue-700">{message}</span>}
      </div>

      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-left text-xs text-gray-500">
            <tr>
              <th className="px-3 py-2">jc</th>
              <th className="px-3 py-2">姓名</th>
              <th className="px-3 py-2">状态</th>
              <th className="px-3 py-2">评分</th>
              <th className="px-3 py-2">截图</th>
              <th className="px-3 py-2">PDF</th>
              <th className="px-3 py-2">最近触达</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.jc_id}
                className="cursor-pointer border-t border-gray-100 hover:bg-blue-50"
                onClick={() => openDetail(row.jc_id)}
              >
                <td className="px-3 py-2">{row.jc_id}</td>
                <td className="px-3 py-2">{row.name}</td>
                <td className="px-3 py-2">{row.status}</td>
                <td className="px-3 py-2">{row.match_score ?? "—"}</td>
                <td className="px-3 py-2">{row.has_snapshot ? "有" : "—"}</td>
                <td className="px-3 py-2">{row.has_pdf ? "有" : "—"}</td>
                <td className="px-3 py-2">{row.last_touch_at ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {detail && (
        <div className="fixed inset-y-0 right-0 z-10 w-[560px] overflow-y-auto border-l border-gray-200 bg-white p-5 shadow-xl">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-lg font-semibold">
              #{detail.jc.id} {detail.candidate.name}
            </h2>
            <button className="text-sm text-gray-500" onClick={() => setDetail(null)}>
              关闭
            </button>
          </div>
          <div className="mb-3 text-sm text-gray-600">
            状态：<b>{detail.jc.status}</b> ｜ 评分：{detail.jc.match_score ?? "—"} ｜{" "}
            {detail.candidate.liepin_user_id}
          </div>
          <p className="mb-4 text-xs text-gray-500">{detail.jc.judge_reason}</p>

          <div className="mb-4">
            <div className="mb-1 text-xs text-gray-500">初筛截图（预签名 ≤15min）</div>
            {detail.snapshot_url ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={detail.snapshot_url} alt="snapshot" className="w-full rounded border" />
            ) : (
              <p className="text-xs text-gray-400">无快照</p>
            )}
          </div>

          <div className="mb-4">
            <div className="mb-1 text-xs text-gray-500">二筛简历 PDF</div>
            {detail.resume_url ? (
              <a href={detail.resume_url} target="_blank" className="text-sm text-blue-600 underline">
                打开 PDF（预签名 ≤15min）
              </a>
            ) : (
              <p className="text-xs text-gray-400">未收到附件</p>
            )}
          </div>

          <div className="mb-4">
            <div className="mb-1 text-xs text-gray-500">在线简历 7 字段</div>
            <pre className="rounded bg-gray-50 p-2 text-xs">
              {JSON.stringify(detail.candidate.online_resume_minimal ?? {}, null, 2)}
            </pre>
          </div>

          <div className="mb-4">
            <div className="mb-1 text-xs text-gray-500">互动时间线</div>
            <ul className="space-y-1 text-xs">
              {detail.interactions.map((row, i) => (
                <li key={i} className="rounded bg-gray-50 px-2 py-1">
                  [{row.sent_at}] {row.direction}/{row.msg_type}：{row.content}
                </li>
              ))}
            </ul>
          </div>

          <div className="flex gap-2">
            <button
              onClick={() => review("approve")}
              className="rounded bg-green-600 px-3 py-1.5 text-sm text-white hover:bg-green-700"
            >
              复核通过
            </button>
            <button
              onClick={() => review("reject")}
              className="rounded bg-red-600 px-3 py-1.5 text-sm text-white hover:bg-red-700"
            >
              复核推翻
            </button>
            <button
              onClick={rerun}
              className="rounded border border-gray-300 px-3 py-1.5 text-sm hover:bg-gray-50"
            >
              手动重跑
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
