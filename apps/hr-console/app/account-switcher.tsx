"use client";

import { useCallback, useEffect, useState } from "react";
import { Button, Input, Modal, Select } from "antd";
import { PlusOutlined, WarningOutlined } from "@ant-design/icons";

type Account = { id: number; name: string; is_active: boolean };

/**
 * 猎聘账号切换器（多账号支持，2026-10-14）。
 *
 * 重要：这里**只是告诉系统「接下来按哪个号算」**，不会去手机上切号——登出/登录
 * 由人工在猎聘 App 里完成。顺序必须是「先在手机切号 → 再在这里点选」。
 *
 * 切号后整页刷新：各页面的数据是客户端 useEffect 里 fetch 的，router.refresh()
 * 不会重新触发那些请求。
 */
export default function AccountSwitcher() {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [switching, setSwitching] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState("");
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

  async function activate(id: number) {
    setSwitching(true);
    try {
      const resp = await fetch(`/api/hr/accounts/${id}/activate`, { method: "POST" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      window.location.reload();
    } catch (e) {
      setErr(`切换失败：${e instanceof Error ? e.message : String(e)}`);
      setSwitching(false);
    }
  }

  async function create() {
    const name = newName.trim();
    if (!name) return;
    try {
      const resp = await fetch("/api/hr/accounts", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      setNewName("");
      setCreateOpen(false);
      await load();
    } catch (e) {
      setErr(`新建失败：${e instanceof Error ? e.message : String(e)}`);
    }
  }

  return (
    <div className="account-switcher">
      <span className="account-switcher-label">猎聘账号</span>
      <Select
        size="small"
        style={{ minWidth: 160 }}
        loading={loading}
        disabled={switching}
        value={activeId ?? undefined}
        placeholder={activeId === null ? "未选择" : undefined}
        onChange={(v) => void activate(v)}
        options={accounts.map((a) => ({ value: a.id, label: a.name }))}
        status={activeId === null ? "error" : undefined}
      />
      <Button
        size="small"
        type="text"
        icon={<PlusOutlined />}
        onClick={() => setCreateOpen(true)}
        title="新建账号"
      />

      {activeId === null && !loading && (
        <span className="account-switcher-warn">
          <WarningOutlined /> 没有生效账号，轮次不会执行
        </span>
      )}
      {err && <span className="account-switcher-warn">{err}</span>}

      <Modal
        title="新建猎聘账号"
        open={createOpen}
        onOk={() => void create()}
        onCancel={() => setCreateOpen(false)}
        okText="创建"
        cancelText="取消"
        destroyOnHidden
      >
        <p style={{ color: "var(--fg-muted, #888)", marginTop: 0 }}>
          名称只是给你自己看的标签（如「主号-张HR」）。创建后还需在列表里点选才会生效。
        </p>
        <Input
          placeholder="账号名称"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          onPressEnter={() => void create()}
          maxLength={64}
        />
      </Modal>
    </div>
  );
}
