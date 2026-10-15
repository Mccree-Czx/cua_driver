"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Input } from "antd";
import { CheckCircleOutlined } from "@ant-design/icons";

type Phase =
  | "idle"
  | "pending"
  | "logging_out" // 切换时：正在退出旧号
  | "dispatched"
  | "awaiting_code"
  | "verifying"
  | "success"
  | "failed"
  | "cancelled"
  | "expired";

type Progress = {
  phase: Phase;
  kind?: "login" | "switch" | null;
  method?: "password" | "code" | null;
  account_id?: number | null;
  account_name?: string | null;
  from_account_id?: number | null;
  from_account_name?: string | null;
  phone_masked?: string | null;
  resend_seq?: number;
  error?: string | null;
  notice?: string | null;
};

const POLL_MS = 3000;
const TERMINAL: Phase[] = ["success", "failed", "cancelled", "expired"];

type AccountLite = {
  id: number;
  name: string;
  phone_masked: string | null;
  has_password: boolean;
};

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
  intent = "login",
  onClose,
  onChanged,
}: {
  account: AccountLite;
  /** login = 手机已在登录页时只登录；switch = 先退出旧号再登录目标号 */
  intent?: "login" | "switch";
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

  // **一直在轮询，不在终态停下。**
  // 之前终态就停，于是外部把请求重置后前端看不见，必须人工点「重试」——
  // 2026-10-15 为此卡了好几轮。代价是弹窗开着时每 3 秒一个请求，可忽略。
  const poll = useCallback(async () => {
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

  async function retry() {
    // 「重试」要**真的重新发起请求**——只重置本地状态的话，外部那条还是 failed，
    // 下一次轮询立刻又把它拉回来，点了等于没点（2026-10-15 就踩了这个）。
    phaseRef.current = "idle";
    setProgress({ phase: "idle" });
    setCode("");
    setErr(null);
    await start();
  }

  /** 切换失败后手机上是没号的。这个按钮用同一条流程登回旧号——不假装自动回滚，
   *  而是给一个明确的一键入口（登回旧号同样要人工填码，没有"自动"可言）。 */
  async function backToSource() {
    const id = progress.from_account_id;
    if (!id) return;
    phaseRef.current = "idle";
    setProgress({ phase: "idle" });
    await post(`/api/hr/accounts/${id}/switch`, { method: "code", force: true });
  }

  async function start() {
    const path = intent === "switch" ? "switch" : "login";
    await post(`/api/hr/accounts/${account.id}/${path}`, { method: "code" });
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
          {intent === "switch" ? (
            <>
              <Alert
                type="warning"
                showIcon
                message="这一步会先退出手机上当前登着的账号"
                description={
                  <>
                    系统会：<b>退出当前账号 → 登录「{account.name}」</b>。
                    退出是<b>不可逆</b>的——如果新号没登上，手机上就没有账号了，
                    流水线会停摆到你重新登回。失败时下面会给一个「登回原账号」的入口。
                    <br />
                    若此刻正有一轮招聘轮次在用手机，系统会<b>中断它</b>——
                    万一它当时正在打招呼，可能有一次没记上账，建议事后在候选人列表里核对一下。
                  </>
                }
              />
              <Checkbox checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)}>
                我确认要退出当前账号，并切换到「{account.name}」
              </Checkbox>
            </>
          ) : (
            <>
              <Alert
                type="warning"
                showIcon
                message="开始之前请先在手机上退出当前账号"
                description={
                  <>
                    这个按钮<b>只负责登录</b>，不会替你退出旧号。
                    请在猎聘 App 里手动退出，停在登录页，再点下面的按钮。
                    （想一步到位就用账号列表里的「切换到此账号」。）
                  </>
                }
              />
              <Checkbox checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)}>
                我已在手机上退出当前账号，并停在登录页
              </Checkbox>
            </>
          )}
          <div className="account-form">
            <Button
              type="primary"
              disabled={!confirmed}
              loading={busy}
              onClick={() => void start()}
            >
              {intent === "switch" ? "开始切换" : "登录此账号"}
            </Button>
          </div>
          <p className="account-form-hint">
            系统会在手机上操作到「验证码已发送」，
            <b>然后这里会弹出输入框让你填验证码</b>。
            <br />
            密码登录已停用——猎聘的账号密码登录点下去会弹滑块验证，实测走不通。
          </p>
        </>
      )}

      {shown === "pending" && (
        <Step text={intent === "switch" ? "正在开始切换…" : "正在手机上打开猎聘…"} />
      )}
      {shown === "logging_out" && (
        <Step text={`正在退出当前账号${progress.from_account_name ? `「${progress.from_account_name}」` : ""}…`} />
      )}
      {shown === "dispatched" && <Step text="正在输入手机号并发出验证码…" />}
      {shown === "verifying" && <Step text="已收到验证码，正在提交…" />}

      {/* 过程说明（黄条）：例如「密码登录失败，已改用验证码」。
          与 error 分开——error 是失败的结论，这是「换路了，告诉你一声」。
          只在属于本账号的请求上显示，别把别人的进度提示挂到自己头上。 */}
      {mine && progress.notice && !done && (
        <Alert type="warning" showIcon message={progress.notice} />
      )}

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

      {/* 切换失败且已退出旧号 → 手机上没号了。给一个明确的一键入口，
          而不是让用户对着「失败」两个字猜下一步。 */}
      {progress.from_account_id && done && shown !== "success" && (
        <Button size="small" type="primary" danger loading={busy} onClick={() => void backToSource()}>
          登回「{progress.from_account_name ?? `账号 ${progress.from_account_id}`}」
        </Button>
      )}

      {err && <Alert type="error" showIcon message={err} />}

      {shown !== "idle" && !done && (
        <Button size="small" onClick={() => void post("/api/hr/accounts/login-request/cancel")}>
          取消这次登录
        </Button>
      )}
      {done && shown !== "success" && (
        <Button size="small" onClick={() => void retry()}>
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
    failed: "没成功",
    cancelled: "已取消",
    expired: "请求已超时",
    logging_out: "退出旧号时出错",
  };
  return map[phase] ?? "未完成";
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
