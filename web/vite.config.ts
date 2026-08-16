import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Dev: proxy the pinned HTTP contract to the Python engine. Override the port with
// API_TARGET=http://127.0.0.1:9000 npm run dev if the backend moves.
const target = process.env.API_TARGET || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target, changeOrigin: true },
    },
  },
})
