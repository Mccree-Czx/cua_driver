"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Input } from "antd";
import { CheckCircleOutlined } from "@ant-design/icons";

type Phase =
  | "idle"
  | "pending"
  | "dispatched"
  | "awaiting_code"
  | "verifying"
  | "success"
  | "failed"
  | "cancelled"
  | "expired";

type Progress = {
  phase: Phase;
  account_id?: number | null;
  account_name?: string | null;
  phone_masked?: string | null;
  resend_seq?: number;
  error?: string | null;
};

const POLL_MS = 3000;
const TERMINAL: Phase[] = ["success", "failed", "cancelled", "expired"];

type AccountLite = { id: number; name: string; phone_masked: string | null };

/**
 * 登录向导（批次 2）。
 *
 * 分工：系统在手机上把猎聘操作到「验证码已发送」，**验证码由人工从自己手机上
 * 读出来填在这里**——短信发到的是用户自己的号，系统物理上读不到。
 *
 * 轮询只在向导打开时进行，不引入全局轮询。
 */
export default function LoginWizard({
  account,
  onClose,
  onChanged,
}: {
  account: AccountLite;
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const [progress, setProgress] = useState<Progress>({ phase: "idle" });
  const [confirmed, setConfirmed] = useState(false);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // 用 ref 判终态：直接依赖 progress 会让 effect 每次都重建定时器
  const phaseRef = useRef<Phase>("idle");

  const poll = useCallback(async () => {
    if (TERMINAL.includes(phaseRef.current)) return;
    try {
      const resp = await fetch("/api/hr/accounts/login-request");
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data: Progress = await resp.json();
      phaseRef.current = data.phase;
      setProgress(data);
      if (data.phase === "success") await onChanged();
    } catch (e) {
      setErr(`读取进度失败：${e instanceof Error ? e.message : String(e)}`);
    }
  }, [onChanged]);

  useEffect(() => {
    void poll();
    const id = window.setInterval(() => void poll(), POLL_MS);
    return () => window.clearInterval(id);
  }, [poll]);

  async function post(path: string, body?: unknown) {
    setBusy(true);
    setErr(null);
    try {
      const resp = await fetch(path, {
        method: "POST",
        headers: body ? { "content-type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      if (!resp.ok) throw new Error(await detailOf(resp));
      const data: Progress = await resp.json();
      phaseRef.current = data.phase;
      setProgress(data);
      if (data.phase === "success") await onChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function retry() {
    // phaseRef 也要一并复位——它才是轮询的开关，只清 state 会让轮询再也回不来
    phaseRef.current = "idle";
    setProgress({ phase: "idle" });
    setConfirmed(false);
    setCode("");
    setErr(null);
  }

  const phase = progress.phase;
  const done = TERMINAL.includes(phase);
  const mine = progress.account_id === account.id;
  // 属于别人的请求不该冒充本向导的进度：终态的（30 分钟 TTL 内一直留在 Redis 里）
  // 直接当没看见，在途的则必须提示——否则用户会对着一个不属于自己的进度发呆。
  const foreignInFlight = !mine && phase !== "idle" && !done;
  const shown: Phase = mine || foreignInFlight ? phase : "idle";

  return (
    <div className="login-wizard">
      <div className="account-manager-heading">
        登录账号 · {account.name}
        <Button size="small" type="link" onClick={onClose}>
          收起
        </Button>
      </div>

      {shown === "idle" && (
        <>
          <Alert
            type="warning"
            showIcon
            message="开始之前请先确认"
            description={
              <>
                手机上登录新号 = 旧号会被登出。<b>系统不会自动登出，也不会在失败后把旧号登回来</b>
                ——请先确认你手上能登回当前这个生产号（密码已存在系统里）。
                然后在猎聘 App 里手动退出当前账号，再点下面的按钮。
              </>
            }
          />
          <Checkbox checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)}>
            我已在手机上退出当前账号，并确认能登回它
          </Checkbox>
          <Button
            type="primary"
            disabled={!confirmed}
            loading={busy}
            onClick={() => void post(`/api/hr/accounts/${account.id}/login`)}
          >
            开始登录
          </Button>
        </>
      )}

      {shown === "pending" && <Step text="正在手机上打开猎聘…" />}
      {shown === "dispatched" && <Step text="正在输入手机号并获取验证码…" />}
      {shown === "verifying" && <Step text="已收到验证码，正在提交…" />}

      {shown === "awaiting_code" && (
        <>
          <Alert
            type="info"
            showIcon
            message={`验证码已发送到 ${progress.phone_masked ?? account.phone_masked ?? "该账号绑定的手机"}`}
            description="请查看你自己的手机短信，把 6 位验证码填在下面。（验证码 5 分钟内有效）"
          />
          <div className="account-form">
            <Input
              placeholder="请输入验证码"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              onPressEnter={() => void post("/api/hr/accounts/login-request/code", { code })}
              maxLength={8}
              inputMode="numeric"
              autoFocus
            />
            <Button
              type="primary"
              loading={busy}
              disabled={!code.trim()}
              onClick={() => void post("/api/hr/accounts/login-request/code", { code })}
            >
              提交验证码
            </Button>
            <Button onClick={() => void post("/api/hr/accounts/login-request/resend")}>
              重发
            </Button>
          </div>
        </>
      )}

      {/* 在途请求属于别的账号：说清楚，别让人对着一个不属于自己的进度发呆 */}
      {foreignInFlight && (
        <Alert
          type="warning"
          showIcon
          message={`当前有另一个账号在登录中（${progress.account_name ?? progress.account_id}）`}
          description="一部手机同一时刻只能登一个号。请先等它结束，或取消后再开始。"
        />
      )}

      {shown === "success" && (
        <Alert
          type="success"
          showIcon
          icon={<CheckCircleOutlined />}
          message="登录成功"
          description={`手机上已登录 ${account.name}，系统也已把它设为当前生效账号。`}
        />
      )}

      {done && shown !== "success" && shown !== "idle" && (
        <Alert
          type="error"
          showIcon
          message={phaseLabel(shown)}
          description={progress.error || "未提供原因"}
        />
      )}

      {err && <Alert type="error" showIcon message={err} />}

      {shown !== "idle" && !done && (
        <Button size="small" onClick={() => void post("/api/hr/accounts/login-request/cancel")}>
          取消这次登录
        </Button>
      )}
      {done && shown !== "success" && (
        <Button size="small" onClick={retry}>
          重试
        </Button>
      )}
    </div>
  );
}

function Step({ text }: { text: string }) {
  return (
    <div className="login-step">
      <span className="login-spinner" aria-hidden />
      {text}
    </div>
  );
}

function phaseLabel(phase: Phase): string {
  const map: Record<string, string> = {
    failed: "登录失败",
    cancelled: "已取消",
    expired: "登录请求已超时",
  };
  return map[phase] ?? "登录未完成";
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
