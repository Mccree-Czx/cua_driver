"use client";

import { use, useState } from "react";
import { io } from "next/cache";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Menu, Drawer } from "antd";
import {
  DashboardOutlined,
  TeamOutlined,
  InboxOutlined,
  FileTextOutlined,
  MenuOutlined,
  RobotOutlined,
} from "@ant-design/icons";
import AlertBar from "./alert-bar";

const NAV = [
  { key: "/", icon: <DashboardOutlined />, label: <Link href="/">驾驶舱</Link> },
  { key: "/candidates", icon: <TeamOutlined />, label: <Link href="/candidates">候选人</Link> },
  { key: "/manual", icon: <InboxOutlined />, label: <Link href="/manual">人工队列</Link> },
  { key: "/logs", icon: <FileTextOutlined />, label: <Link href="/logs">运行日志</Link> },
];

function BrandMark() {
  return (
    <div className="brand">
      <span className="brand-icon-box">
        <RobotOutlined style={{ fontSize: 18, color: "var(--cold-brew-dark)" }} />
      </span>
      <span>OutIn HR Portal</span>
    </div>
  );
}

export default function AppShell({ children }: { children: React.ReactNode }) {
  // antd 的 CSS-in-JS 在渲染时用 Math.random() 生成样式 seed，SSR 预渲染会报
  // blocking-prerender 错误。用 use(io()) 挂起预渲染，让整棵 antd 树只在浏览器渲染。
  use(io());

  const pathname = usePathname();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const selectedKey =
    NAV.find((n) => n.key !== "/" && pathname.startsWith(n.key))?.key ?? "/";

  const navMenu = (
    <Menu
      mode="inline"
      selectedKeys={[selectedKey]}
      items={NAV}
      onClick={() => setDrawerOpen(false)}
    />
  );

  return (
    <div className="portal-shell">
      <aside className="portal-sider">
        <BrandMark />
        <div className="workspace-label">工作空间</div>
        {navMenu}
      </aside>

      <div className="portal-main-layout">
        <div className="portal-mobile-header">
          <div className="mobile-brand">
            <span className="brand-icon-box">
              <RobotOutlined style={{ fontSize: 18, color: "var(--cold-brew-dark)" }} />
            </span>
            <span className="mobile-brand-title">OutIn HR Portal</span>
          </div>
          <button className="mobile-menu-btn" onClick={() => setDrawerOpen(true)} aria-label="菜单">
            <MenuOutlined />
          </button>
        </div>

        <Drawer
          className="mobile-nav-drawer"
          placement="left"
          open={drawerOpen}
          onClose={() => setDrawerOpen(false)}
          width={216}
          closable={false}
          styles={{ body: { padding: 0 } }}
        >
          <div className="mobile-drawer-inner">
            <BrandMark />
            <div className="workspace-label">工作空间</div>
            {navMenu}
          </div>
        </Drawer>

        <div className="portal-content">
          <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 12 }}>
            <AlertBar />
          </div>
          {children}
        </div>
      </div>
    </div>
  );
}
