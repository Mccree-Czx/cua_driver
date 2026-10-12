"use client";

import { useEffect, useState } from "react";

type Alerts = {
  login?: { is_login?: boolean | null; checked_at?: string | null } | null;
  risk_paused?: boolean;
  quota_used_today?: number;
  manual_today?: number;
};

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
  if (alerts.risk_paused) flags.push({ text: "全局熔断中", cls: "status-capsule-danger" });
  if (alerts.login && alerts.login.is_login === false)
    flags.push({ text: "登录失效", cls: "status-capsule-pending" });
  flags.push({ text: `今日触达 ${alerts.quota_used_today ?? 0}`, cls: "status-capsule-contacting" });
  if ((alerts.manual_today ?? 0) > 0)
    flags.push({ text: `转人工 ${alerts.manual_today}`, cls: "status-capsule-running" });

  return (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
      {flags.map((f) => (
        <span key={f.text} className={`capsule-pill ${f.cls}`}>
          <span className="capsule-pill-dot" />
          {f.text}
        </span>
      ))}
    </div>
  );
}
