import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// dev 环境走 Vite 代理直连后端（SSE 经 http-proxy 不缓冲，无 CORS 顾虑）
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8100",
        changeOrigin: true,
      },
    },
  },
});
