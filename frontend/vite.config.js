import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 本地调试：前端走相对路径，由 vite dev server 代理到后端 127.0.0.1:7013，
// 浏览器同源，避免 CORS / WebSocket 问题。
// 线上部署时前端用绝对 URL 直连后端（见 src/api.js）。
const API_PREFIX = '/ocrsys-7e2f9a4d1c8b3e6f5a0d2b9c7e4f1a8d3b6e9c0d5a2f7b8e1c4d6a9f3b5e0c2d'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      [API_PREFIX]: {
        target: 'http://127.0.0.1:7013',
        changeOrigin: true,
        ws: true,
      },
    },
  },
})
