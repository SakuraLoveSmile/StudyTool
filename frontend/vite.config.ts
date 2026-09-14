import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    vue({
      // <feedback-widget> 由 @feedback/web 在运行时注册的 Custom Element，不是 Vue 组件。
      // 不声明 isCustomElement 时模板编译器会生成 resolveComponent('feedback-widget')，
      // 开发模式报 "Failed to resolve component: feedback-widget"。
      template: {
        compilerOptions: {
          isCustomElement: (tag: string) => tag === 'feedback-widget',
        },
      },
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5174,
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
