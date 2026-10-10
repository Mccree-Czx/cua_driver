"use client";

import { use } from "react";
import { io } from "next/cache";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Layout, Menu } from "antd";
import {
  DashboardOutlined,
  TeamOutlined,
  InboxOutlined,
  FileTextOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import AlertBar from "./alert-bar";

const { Sider, Header, Content } = Layout;

const NAV = [
  { key: "/", icon: <DashboardOutlined />, label: <Link href="/">驾驶舱</Link> },
  { key: "/candidates", icon: <TeamOutlined />, label: <Link href="/candidates">候选人</Link> },
  { key: "/manual", icon: <InboxOutlined />, label: <Link href="/manual">人工队列</Link> },
  { key: "/logs", icon: <FileTextOutlined />, label: <Link href="/logs">运行日志</Link> },
  { key: "/scoring", icon: <SettingOutlined />, label: <Link href="/scoring">评分偏好</Link> },
];

export default function AppShell({ children }: { children: React.ReactNode }) {
  // antd 的 CSS-in-JS 在渲染时用 Math.random() 生成样式 seed，SSR 预渲染会报
  // blocking-prerender 错误。用 use(io()) 挂起预渲染，让整棵 antd 树只在浏览器渲染。
  use(io());

  const pathname = usePathname();
  const selectedKey =
    NAV.find((n) => n.key !== "/" && pathname.startsWith(n.key))?.key ?? "/";

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider theme="light" width={220}>
        <div
          style={{
            height: 56,
            display: "flex",
            alignItems: "center",
            paddingInline: 20,
            fontWeight: 700,
            fontSize: 16,
            borderBottom: "1px solid #f0f0f0",
          }}
        >
          hr-workbuddy
        </div>
        <Menu mode="inline" selectedKeys={[selectedKey]} items={NAV} style={{ borderInlineEnd: "none" }} />
      </Sider>
      <Layout>
        <Header
          style={{
            background: "#fff",
            display: "flex",
            alignItems: "center",
            justifyContent: "flex-end",
            paddingInline: 24,
            borderBottom: "1px solid #f0f0f0",
          }}
        >
          <AlertBar />
        </Header>
        <Content style={{ margin: 24 }}>{children}</Content>
      </Layout>
    </Layout>
  );
}
