"use client";

import { useEffect, useState } from "react";

type Overview = {
  job?: { id: number; title: string; llm_threshold: number } | null;
  status_counts: Record<string, number>;
  score_buckets: Record<string, number>;
  today: { touches_out: number; received: number; received_target?: number; manual: number };
};

type DailyDay = {
  date: string;
  new_jc: number;
  direct_request: number;
  greet_request: number;
  reply: number;
  received: number;
  conversion: number | null;
  tokens: number;
  cost: number;
  manual: number;
};

type Job = { id: number; title: string; llm_threshold: number; status: string };

export default function Home() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [daily, setDaily] = useState<{ days: DailyDay[]; totals: Record<string, number | null> } | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [thresholdDraft, setThresholdDraft] = useState<Record<number, number>>({});
  const [message, setMessage] = useState("");

  function load() {
    fetch("/api/hr/overview").then((r) => r.json()).then(setOverview).catch(() => {});
    fetch("/api/hr/daily?days=14").then((r) => r.json()).then(setDaily).catch(() => {});
    fetch("/api/jobs")
      .then((r) => r.json())
      .then((body) => setJobs(body.items ?? body ?? []))
      .catch(() => {});
  }

  useEffect(load, []);

  async function saveThreshold(jobId: number) {
    const value = thresholdDraft[jobId];
    if (value === undefined) return;
    const resp = await fetch(`/api/hr/jobs/${jobId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ llm_threshold: value }),
    });
    setMessage(resp.ok ? `岗位 ${jobId} 阈值已更新为 ${value}` : `更新失败（HTTP ${resp.status}）`);
    load();
  }

  return (
    <div className="space-y-8">
      <section>
        <h1 className="mb-4 text-xl font-semibold">漏斗总览</h1>
        {overview ? (
          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            <div className="rounded-lg border border-gray-200 bg-white p-4">
              <div className="text-xs text-gray-500">今日触达（out）</div>
              <div className="text-2xl font-semibold">{overview.today.touches_out}</div>
            </div>
            <div className="rounded-lg border border-gray-200 bg-white p-4">
              <div className="text-xs text-gray-500">今日收到简历</div>
              <div className="text-2xl font-semibold">{overview.today.received}</div>
              <div className="mt-1 text-xs text-gray-400">
                目标 {overview.today.received_target ?? 50} / 天
              </div>
              <div className="mt-1 h-1.5 w-full rounded bg-gray-100">
                <div
                  className="h-1.5 rounded bg-blue-500"
                  style={{
                    width: `${Math.min(
                      100,
                      Math.round(
                        (overview.today.received / (overview.today.received_target ?? 50)) * 100
                      )
                    )}%`,
                  }}
                />
              </div>
            </div>
            <div className="rounded-lg border border-gray-200 bg-white p-4">
              <div className="text-xs text-gray-500">今日转人工</div>
              <div className="text-2xl font-semibold">{overview.today.manual}</div>
            </div>
            <div className="rounded-lg border border-gray-200 bg-white p-4">
              <div className="text-xs text-gray-500">候选人总数</div>
              <div className="text-2xl font-semibold">
                {Object.values(overview.status_counts).reduce((a, b) => a + b, 0)}
              </div>
            </div>
            <div className="col-span-full rounded-lg border border-gray-200 bg-white p-4">
              <div className="mb-2 text-xs text-gray-500">状态分布</div>
              <div className="flex flex-wrap gap-3 text-sm">
                {Object.entries(overview.status_counts).map(([k, v]) => (
                  <span key={k} className="rounded bg-gray-100 px-2 py-1">
                    {k}: <b>{v}</b>
                  </span>
                ))}
              </div>
              <div className="mt-3 mb-2 text-xs text-gray-500">评分分布</div>
              <div className="flex flex-wrap gap-3 text-sm">
                {Object.entries(overview.score_buckets).map(([k, v]) => (
                  <span key={k} className="rounded bg-gray-100 px-2 py-1">
                    {k}: <b>{v}</b>
                  </span>
                ))}
              </div>
            </div>
          </div>
        ) : (
          <p className="text-sm text-gray-500">加载中…（需 pipeline 在 127.0.0.1:8000 运行）</p>
        )}
      </section>

      <section>
        <h2 className="mb-3 text-lg font-semibold">每日漏斗（近 14 天）</h2>
        <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-xs text-gray-500">
              <tr>
                <th className="px-3 py-2">日期</th>
                <th className="px-3 py-2">新增</th>
                <th className="px-3 py-2">直索要</th>
                <th className="px-3 py-2">打招呼</th>
                <th className="px-3 py-2">回执</th>
                <th className="px-3 py-2">收到简历</th>
                <th className="px-3 py-2">转化率</th>
                <th className="px-3 py-2">tokens</th>
                <th className="px-3 py-2">转人工</th>
              </tr>
            </thead>
            <tbody>
              {(daily?.days ?? []).map((d) => (
                <tr key={d.date} className="border-t border-gray-100">
                  <td className="px-3 py-2">{d.date}</td>
                  <td className="px-3 py-2">{d.new_jc}</td>
                  <td className="px-3 py-2">{d.direct_request}</td>
                  <td className="px-3 py-2">{d.greet_request}</td>
                  <td className="px-3 py-2">{d.reply}</td>
                  <td className="px-3 py-2">{d.received}</td>
                  <td className="px-3 py-2">{d.conversion === null ? "—" : `${d.conversion}%`}</td>
                  <td className="px-3 py-2">{d.tokens}</td>
                  <td className="px-3 py-2">{d.manual}</td>
                </tr>
              ))}
            </tbody>
            {daily && (
              <tfoot className="bg-gray-50 text-xs">
                <tr>
                  <td className="px-3 py-2 font-medium">合计</td>
                  <td className="px-3 py-2">{daily.totals.new_jc}</td>
                  <td className="px-3 py-2">—</td>
                  <td className="px-3 py-2">—</td>
                  <td className="px-3 py-2">—</td>
                  <td className="px-3 py-2">{daily.totals.received}</td>
                  <td className="px-3 py-2">
                    {daily.totals.conversion === null ? "—" : `${daily.totals.conversion}%`}
                  </td>
                  <td className="px-3 py-2">{daily.totals.tokens}</td>
                  <td className="px-3 py-2">{daily.totals.manual}</td>
                </tr>
              </tfoot>
            )}
          </table>
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-lg font-semibold">岗位阈值调整（阈值回流）</h2>
        <div className="space-y-2 rounded-lg border border-gray-200 bg-white p-4">
          {jobs.length === 0 && <p className="text-sm text-gray-500">暂无岗位</p>}
          {jobs.map((job) => (
            <div key={job.id} className="flex items-center gap-3 text-sm">
              <span className="w-64 truncate">
                #{job.id} {job.title}（{job.status}）
              </span>
              <input
                type="number"
                min={0}
                max={100}
                className="w-20 rounded border border-gray-300 px-2 py-1"
                value={thresholdDraft[job.id] ?? job.llm_threshold}
                onChange={(e) =>
                  setThresholdDraft({ ...thresholdDraft, [job.id]: Number(e.target.value) })
                }
              />
              <button
                onClick={() => saveThreshold(job.id)}
                className="rounded bg-blue-600 px-3 py-1 text-white hover:bg-blue-700"
              >
                保存
              </button>
            </div>
          ))}
          {message && <p className="text-xs text-gray-600">{message}</p>}
        </div>
      </section>
    </div>
  );
}
