import type { Metadata } from "next";
import { Suspense } from "react";
import "./globals.css";
import AppShell from "./app-shell";

export const metadata: Metadata = {
  title: "BOSS 招聘控制台",
  description: "BOSS直聘 自动化招聘工作台",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <Suspense fallback={<div style={{ minHeight: "100vh" }} />}>
          <AppShell>{children}</AppShell>
        </Suspense>
      </body>
    </html>
  );
}
