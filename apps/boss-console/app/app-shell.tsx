"use client";

import { use, useState } from "react";
import { io } from "next/cache";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Menu, Drawer } from "antd";
import {
  DashboardOutlined,
  ProfileOutlined,
  TeamOutlined,
  MessageOutlined,
  InboxOutlined,
  FileTextOutlined,
  SlidersOutlined,
  MenuOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import AlertBar from "./alert-bar";

/**
 * BOSS 控制台的外壳。
 *
 * ★ 跟猎聘的 `hr-console` 是**同一个设计系统**（globals.css 直接复用），
 *   差别只在导航项和品牌名。这是有意的：后面「统一外壳 + 统一导航」那条线
 *   要把两套并到一个门户里，现在长得一样，到时候才好合。
 *
 * 这里**没有账号切换器、没有告警条**——猎聘那两个组件绑的是它的登录态与配额，
 * BOSS 这边还没有对应的东西。等统一身份那条线做完再说。
 */
/**
 * 导航。★ 顺序照「人一天里怎么用」排，不是照字母：
 * 先看驾驶舱，再看职位（今天招什么），再是候选人（谁在投），
 * 然后会话（谁回话了），人工队列（哪里卡住了），日志（出什么事了），
 * 评分偏好排最后（现在还没接打分，是保留形状的页）。
 *
 * 没有「账号」相关项——BOSS 无密码、单账号（用户已定不复刻那套）。
 */
const NAV = [
  { key: "/", icon: <DashboardOutlined />, label: <Link href="/">驾驶舱</Link> },
  { key: "/jobs", icon: <ProfileOutlined />, label: <Link href="/jobs">职位</Link> },
  { key: "/candidates", icon: <TeamOutlined />, label: <Link href="/candidates">候选人</Link> },
  { key: "/conversations", icon: <MessageOutlined />, label: <Link href="/conversations">会话</Link> },
  { key: "/manual", icon: <InboxOutlined />, label: <Link href="/manual">人工队列</Link> },
  { key: "/logs", icon: <FileTextOutlined />, label: <Link href="/logs">运行日志</Link> },
  { key: "/scoring", icon: <SlidersOutlined />, label: <Link href="/scoring">评分偏好</Link> },
];

function BrandMark() {
  return (
    <div className="brand">
      <span className="brand-icon-box">
        <ThunderboltOutlined style={{ fontSize: 18, color: "var(--cold-brew-dark)" }} />
      </span>
      <span>OutIn BOSS 控制台</span>
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
              <ThunderboltOutlined style={{ fontSize: 18, color: "var(--cold-brew-dark)" }} />
            </span>
            <span className="mobile-brand-title">OutIn BOSS 控制台</span>
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
          {/* 顶栏只留告警条——猎聘那边的「账号切换器」在 BOSS 上没有对应物 */}
          <div className="portal-topbar">
            <AlertBar />
          </div>
          {children}
        </div>
      </div>
    </div>
  );
}
