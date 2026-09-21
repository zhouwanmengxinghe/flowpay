/* 配置 React、Tailwind、路径别名与后端代理。 */
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath, URL } from 'node:url'

// 容器里后端在另一个 service 上，用 FLOWPAY_API_PROXY 指过去；宿主机直跑时留空即可。
const api = process.env.FLOWPAY_API_PROXY ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    port: 5173,
    strictPort: true,
    proxy: { '/api': api },
    // 挂载卷在部分平台上收不到 inotify 事件，容器里改用轮询。
    watch: process.env.FLOWPAY_POLL_WATCH ? { usePolling: true, interval: 300 } : undefined,
  },
})
