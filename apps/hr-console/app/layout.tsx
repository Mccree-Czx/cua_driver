import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import AlertBar from "./alert-bar";

export const metadata: Metadata = {
  title: "hr-workbuddy 工作台",
  description: "HR 候选人工作台（M3 最小可用 / M4 可观测）",
};

const NAV = [
  { href: "/", label: "总览" },
  { href: "/candidates", label: "候选人" },
  { href: "/manual", label: "人工队列" },
];

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="zh-CN" className="h-full antialiased">
      <body className="min-h-full bg-gray-50 text-gray-900">
        <header className="border-b border-gray-200 bg-white">
          <div className="mx-auto flex max-w-6xl items-center gap-6 px-6 py-3">
            <span className="text-lg font-semibold">hr-workbuddy</span>
            <nav className="flex gap-4 text-sm">
              {NAV.map((item) => (
                <Link key={item.href} href={item.href} className="hover:text-blue-600">
                  {item.label}
                </Link>
              ))}
            </nav>
            <div className="ml-auto">
              <AlertBar />
            </div>
          </div>
        </header>
        <main className="mx-auto max-w-6xl p-6">{children}</main>
      </body>
    </html>
  );
}
