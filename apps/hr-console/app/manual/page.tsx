"use client";

import { useCallback, useEffect, useState } from "react";

type QueueItem = {
  task_id: string;
  task_type: string | null;
  job_candidate_id: number | null;
  candidate_name: string | null;
  jc_status: string | null;
  attempt: number;
  note: string | null;
  created_at: string | null;
};

export default function ManualQueue() {
  const [items, setItems] = useState<QueueItem[]>([]);
  const [message, setMessage] = useState("");

  const load = useCallback(() => {
    fetch("/api/hr/manual-queue")
      .then((r) => r.json())
      .then((body) => setItems(body.items ?? []))
      .catch(() => setItems([]));
  }, []);

  useEffect(load, [load]);

  async function ack(taskId: string) {
    const resp = await fetch(`/api/hr/manual-queue/${taskId}/ack`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note: "工作台已确认" }),
    });
    setMessage(resp.ok ? "已注记（acked）" : `注记失败（HTTP ${resp.status}）`);
    load();
  }

  async function rerun(jcId: number | null) {
    if (jcId === null) return;
    const resp = await fetch(`/api/hr/candidates/${jcId}/rerun`, { method: "POST" });
    const body = await resp.json().catch(() => ({}));
    setMessage(resp.ok ? `已派发重跑：${body.type}` : `重跑失败（HTTP ${resp.status}）`);
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold">人工队列</h1>
        <span className="text-xs text-gray-500">{items.length} 项（failed_needs_manual 落账）</span>
        {message && <span className="text-xs text-blue-700">{message}</span>}
      </div>
      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-left text-xs text-gray-500">
            <tr>
              <th className="px-3 py-2">时间</th>
              <th className="px-3 py-2">任务类型</th>
              <th className="px-3 py-2">候选人</th>
              <th className="px-3 py-2">状态</th>
              <th className="px-3 py-2">注记</th>
              <th className="px-3 py-2">操作</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.task_id} className="border-t border-gray-100">
                <td className="px-3 py-2 text-xs">{item.created_at}</td>
                <td className="px-3 py-2">{item.task_type ?? "（登记过期）"}</td>
                <td className="px-3 py-2">
                  {item.candidate_name ?? "—"}
                  {item.job_candidate_id !== null ? `（jc=${item.job_candidate_id}）` : ""}
                </td>
                <td className="px-3 py-2">{item.jc_status ?? "—"}</td>
                <td className="px-3 py-2 text-xs">{item.note ?? "—"}</td>
                <td className="space-x-2 px-3 py-2">
                  {item.job_candidate_id !== null && (
                    <button
                      onClick={() => rerun(item.job_candidate_id)}
                      className="rounded border border-gray-300 px-2 py-1 text-xs hover:bg-gray-50"
                    >
                      重跑
                    </button>
                  )}
                  <button
                    onClick={() => ack(item.task_id)}
                    className="rounded border border-gray-300 px-2 py-1 text-xs hover:bg-gray-50"
                  >
                    忽略
                  </button>
                </td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-6 text-center text-xs text-gray-400">
                  队列为空
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
