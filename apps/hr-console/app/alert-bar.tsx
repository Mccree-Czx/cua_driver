"use client";

import { useEffect, useState } from "react";

type Alerts = {
  login?: { is_login?: boolean | null; checked_at?: string | null } | null;
  risk_paused?: boolean;
  quota_used_today?: number;
  manual_today?: number;
};

/** 顶部告警条（M4 最小）：登录态 / 全局熔断 / 今日配额与转人工。 */
export default function AlertBar() {
  const [alerts, setAlerts] = useState<Alerts | null>(null);

  useEffect(() => {
    fetch("/api/hr/alerts")
      .then((r) => (r.ok ? r.json() : null))
      .then(setAlerts)
      .catch(() => setAlerts(null));
  }, []);

  if (!alerts) return null;

  const flags: { text: string; cls: string }[] = [];
  if (alerts.risk_paused) flags.push({ text: "全局熔断中", cls: "bg-red-100 text-red-700" });
  if (alerts.login && alerts.login.is_login === false)
    flags.push({ text: "登录失效", cls: "bg-amber-100 text-amber-800" });
  flags.push({
    text: `今日触达 ${alerts.quota_used_today ?? 0}`,
    cls: "bg-gray-100 text-gray-600",
  });
  if ((alerts.manual_today ?? 0) > 0)
    flags.push({
      text: `转人工 ${alerts.manual_today}`,
      cls: "bg-amber-100 text-amber-800",
    });

  return (
    <div className="flex gap-2">
      {flags.map((f) => (
        <span key={f.text} className={`rounded px-2 py-0.5 text-xs ${f.cls}`}>
          {f.text}
        </span>
      ))}
    </div>
  );
}
