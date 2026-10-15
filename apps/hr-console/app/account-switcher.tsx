"use client";

import { useCallback, useEffect, useState } from "react";
import { Button, Input, Modal, Tag } from "antd";
import { PlusOutlined, SettingOutlined, WarningOutlined } from "@ant-design/icons";
import LoginWizard from "./login-wizard";

type Account = {
  id: number;
  name: string;
  is_active: boolean;
  phone_masked: string | null;
  has_password: boolean;
};

/**
 * 猎聘账号切换器（多账号支持，2026-10-14；2026-10-15 改）。
 *
 * 这里只**显示**当前生效账号，换号走右边齿轮里的「切换到此账号」——
 * 那条路会真的去手机上退出旧号、登新号，成功之后才改生效账号。
 *
 * ★ 曾经这里是个下拉框，onChange 直接调 `POST /accounts/{id}/activate`。
 *   那个端点只翻库里的 `is_active`、**不碰手机**，一次误触就能让
 *   「库里的生效账号」和「手机上登的号」不一致——而系统里所有其他地方都假设
 *   这两者一致（Pi 按生效账号取职位、算配额、隔离数据）。
 *   端点已删，下拉框一并去掉：**换号只能走一条路**。
 */
export default function AccountSwitcher() {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [manageOpen, setManageOpen] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const resp = await fetch("/api/hr/accounts");
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setAccounts(data.items ?? []);
      setActiveId(data.active_id ?? null);
      setErr(null);
    } catch (e) {
      setErr(`读取账号失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const active = accounts.find((a) => a.id === activeId) ?? null;

  return (
    <div className="account-switcher">
      <span className="account-switcher-label">猎聘账号</span>
      {/* 只读展示。换号请点右边的齿轮 →「切换到此账号」。 */}
      <span
        style={{
          minWidth: 170,
          fontWeight: activeId === null ? 400 : 600,
          color: activeId === null ? "#cf1322" : "inherit",
        }}
        title="换号请点右边的齿轮"
      >
        {loading ? "读取中…" : active ? labelOf(active) : "未选择"}
      </span>
      <Button
        size="small"
        type="text"
        icon={<SettingOutlined />}
        onClick={() => setManageOpen(true)}
        title="账号管理"
      />

      {activeId === null && !loading && (
        <span className="account-switcher-warn">
          <WarningOutlined /> 没有生效账号，轮次不会执行
        </span>
      )}
      {/* 生效账号没登记手机号 → 系统无法核验「手机上的号是不是这个」 */}
      {active && !active.phone_masked && !loading && (
        <span className="account-switcher-warn">
          <WarningOutlined /> 该账号未登记手机号，无法核验
        </span>
      )}
      {err && <span className="account-switcher-warn">{err}</span>}

      <Modal
        title="猎聘账号管理"
        open={manageOpen}
        onCancel={() => setManageOpen(false)}
        footer={null}
        width={620}
        destroyOnHidden
      >
        <AccountManager accounts={accounts} onChanged={load} />
      </Modal>
    </div>
  );
}

function labelOf(a: Account): string {
  return a.phone_masked ? `${a.name}（${a.phone_masked}）` : `${a.name}（未登记手机号）`;
}

type FormState = { name: string; phone: string; password: string };

const EMPTY: FormState = { name: "", phone: "", password: "" };

/**
 * 账号管理弹窗：列表 + 新建/补录。
 *
 * 手机号与密码是账号的凭据。系统用手机号核对「手机上登录的到底是不是这个号」，
 * 密码加密存档——它的用途是换号失败时的回滚保障（旧号登不回去时，靠它重登）。
 */
function AccountManager({
  accounts,
  onChanged,
}: {
  accounts: Account[];
  onChanged: () => Promise<void>;
}) {
  const [editingId, setEditingId] = useState<number | null>(null);
  const [loginFor, setLoginFor] = useState<{ account: Account; intent: "login" | "switch" } | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  function startCreate() {
    setEditingId(null);
    setLoginFor(null);
    setForm(EMPTY);
    setErr(null);
    setMsg(null);
  }

  function startEdit(a: Account) {
    setEditingId(a.id);
    setLoginFor(null);
    // 不回填手机号与密码：接口只回打码手机号，回填就意味着要原样返回真号。
    // 留空即不修改，这是后端 PATCH 的语义。
    setForm({ name: a.name, phone: "", password: "" });
    setErr(null);
    setMsg(null);
  }

  function startLogin(a: Account, intent: "login" | "switch") {
    setLoginFor({ account: a, intent });
    setEditingId(null);
    setErr(null);
    setMsg(null);
  }

  async function submit() {
    setBusy(true);
    setErr(null);
    setMsg(null);
    try {
      const creating = editingId === null;
      if (creating) {
        if (!form.name.trim() || !form.phone.trim() || !form.password) {
          throw new Error("名称、手机号、密码都要填");
        }
        const resp = await fetch("/api/hr/accounts", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            name: form.name.trim(),
            phone: form.phone.trim(),
            password: form.password,
          }),
        });
        if (!resp.ok) throw new Error(await detailOf(resp));
        setMsg("账号已创建。接下来要在手机上登录它，登录完成后回到这里点选。");
      } else {
        // 只提交填了的字段——PATCH 的语义是「未传即不改」
        const body: Record<string, string> = {};
        if (form.name.trim()) body.name = form.name.trim();
        if (form.phone.trim()) body.phone = form.phone.trim();
        if (form.password) body.password = form.password;
        if (Object.keys(body).length === 0) throw new Error("没有要修改的内容");
        const resp = await fetch(`/api/hr/accounts/${editingId}`, {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!resp.ok) throw new Error(await detailOf(resp));
        setMsg("已保存。");
      }
      setForm(EMPTY);
      setEditingId(null);
      await onChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const editing = accounts.find((a) => a.id === editingId) ?? null;

  return (
    <div className="account-manager">
      <div className="account-manager-section">
        <div className="account-manager-heading">已有账号</div>
        <div className="account-list">
          {accounts.map((a) => (
            <div className="account-row" key={a.id}>
              <div className="account-row-main">
                <span className="account-row-name">{a.name}</span>
                {a.is_active && <Tag color="green">生效中</Tag>}
              </div>
              <div className="account-row-meta">
                <span>手机号：{a.phone_masked ?? "未登记"}</span>
                <span>密码：{a.has_password ? "已存（加密）" : "未存"}</span>
              </div>
              {a.is_active ? (
                // 手机上登着的就是它——给「切换」会白退出一次、还白费一条短信
                <Button size="small" disabled title="手机上现在登着的就是它">
                  当前生效
                </Button>
              ) : (
                <>
                  <Button
                    size="small"
                    type="primary"
                    disabled={!a.phone_masked}
                    title={
                      a.phone_masked
                        ? "退出手机上当前的账号，再登录它"
                        : "先补录手机号才能切换"
                    }
                    onClick={() => startLogin(a, "switch")}
                  >
                    切换到此账号
                  </Button>
                  <Button
                    size="small"
                    disabled={!a.phone_masked}
                    title="手机上已经登出时用这个——它不会替你退出旧号"
                    onClick={() => startLogin(a, "login")}
                  >
                    仅登录
                  </Button>
                </>
              )}
              <Button size="small" onClick={() => startEdit(a)}>
                补录凭据
              </Button>
            </div>
          ))}
        </div>
      </div>

      {loginFor ? (
        <div className="account-manager-section">
          <LoginWizard
            account={loginFor.account}
            intent={loginFor.intent}
            onClose={() => setLoginFor(null)}
            onChanged={onChanged}
          />
        </div>
      ) : (
        <div className="account-manager-section">
        <div className="account-manager-heading">
          {editing ? `补录凭据 · ${editing.name}` : "新建账号"}
          {editing && (
            <Button size="small" type="link" onClick={startCreate}>
              改为新建
            </Button>
          )}
        </div>
        <p className="account-form-hint">
          {editing
            ? "留空的字段不会被修改。手机号与密码不会回显——需要改哪项就重填哪项。"
            : "手机号是账号的真实身份，系统靠它核验手机上登录的是不是这个号。密码加密存档，用于换号失败时登回旧号。"}
        </p>
        <p className="account-form-hint">
          密码限制：系统要把密码敲进手机，而输入通道敲不了
          <b>双引号</b>和<b>中文</b>——请换掉这两个；其余字母、数字、常见符号都可以。
        </p>
        <div className="account-form">
          <Input
            placeholder={editing ? "账号名称（留空不改）" : "账号名称，如 主号-张HR"}
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            maxLength={64}
          />
          <Input
            placeholder={editing ? "手机号（留空不改）" : "猎聘账号绑定的手机号"}
            value={form.phone}
            onChange={(e) => setForm({ ...form, phone: e.target.value })}
            maxLength={32}
          />
          <Input.Password
            placeholder={editing ? "密码（留空不改）" : "猎聘账号密码"}
            value={form.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })}
            maxLength={128}
            autoComplete="new-password"
          />          <Button type="primary" loading={busy} onClick={() => void submit()}>
            {editing ? "保存" : "创建账号"}
          </Button>
        </div>
        {msg && <p className="account-form-ok">{msg}</p>}
        {err && <p className="account-form-err">{err}</p>}
        </div>
      )}
    </div>
  );
}

/** 把后端的 detail 抠出来给用户看——只报 HTTP 404 等于没说。 */
async function detailOf(resp: Response): Promise<string> {
  try {
    const data = await resp.json();
    if (typeof data?.detail === "string") return data.detail;
  } catch {
    // 响应不是 JSON，退回状态码
  }
  return `HTTP ${resp.status}`;
}
