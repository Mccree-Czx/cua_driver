"use client";

import { useEffect, useState } from "react";

/**
 * 顶栏告警胶囊。
 *
 * 结构照 hr-console 的 alert-bar，但**数据源换成了 BOSS 的**：
 *
 * | 猎聘 | BOSS |
 * |---|---|
 * | 登录失效（账号密码过期） | **等你过风控关卡**（滑块/短信/人脸） |
 * | 全局熔断中 | 同名（风控熔断） |
 * | 今日触达 N | 同名（每日配额） |
 * | 转人工 N | 同名（掉登录/采集失败落在这里） |
 *
 * ★ BOSS 那条「等你过风控关卡」是**猎聘没有的**，而且必须显眼：
 *   手机停在那三关前时，系统什么都做不了——人如果不知道是在等自己，
 *   只会觉得「怎么不动了」。
 */
type Alerts = {
  risk_paused?: boolean;
  risk_reason?: string;
  login_gate_in_flight?: boolean;
  quota_used_today?: number;
  quota_cap?: number;
  manual_today?: number;
};

export default function AlertBar() {
  const [alerts, setAlerts] = useState<Alerts | null>(null);

  useEffect(() => {
    const load = () =>
      fetch("/api/hr/alerts")
        .then((r) => (r.ok ? r.json() : null))
        .then(setAlerts)
        .catch(() => setAlerts(null));
    void load();
    // 告警要新鲜——猎聘那边是单次 fetch（换号才刷新），这里每 30 秒拉一次
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, []);

  if (!alerts) return null;

  const flags: { text: string; cls: string; title?: string }[] = [];
  if (alerts.risk_paused)
    flags.push({
      text: "全局熔断中",
      cls: "status-capsule-danger",
      title: alerts.risk_reason || "撞到风控，所有写动作已停手",
    });
  if (alerts.login_gate_in_flight)
    flags.push({
      text: "等你过风控关卡",
      cls: "status-capsule-pending",
      title: "手机停在滑块/短信/人脸页上，需要你本人在手机上过一下",
    });
  flags.push({
    text: `今日打招呼 ${alerts.quota_used_today ?? 0}/${alerts.quota_cap ?? "?"}`,
    cls: "status-capsule-contacting",
  });
  if ((alerts.manual_today ?? 0) > 0)
    flags.push({ text: `待人工 ${alerts.manual_today}`, cls: "status-capsule-running" });

  return (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
      {flags.map((f) => (
        <span key={f.text} className={`capsule-pill ${f.cls}`} title={f.title}>
          <span className="capsule-pill-dot" />
          {f.text}
        </span>
      ))}
    </div>
  );
}
