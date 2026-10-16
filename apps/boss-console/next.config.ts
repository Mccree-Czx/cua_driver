import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  // 本机联调：/api/* 直代理到 boss-pipeline（免 CORS）。
  // ★ 端口是 **8100**，跟猎聘的 8000 错开——两套会跑在同一台机器上。
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://127.0.0.1:8100/api/:path*",
      },
    ];
  },
};

export default nextConfig;
