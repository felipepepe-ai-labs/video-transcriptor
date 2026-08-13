import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    proxy: {
      '/config': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
      '/transcribe': 'http://localhost:8000',
      // catch-all for everything else the frontend requests
      '/jobs': 'http://localhost:8000',
      '/upload': 'http://localhost:8000',
      '/x': 'http://localhost:8000',
      '/audio': 'http://localhost:8000',
      '/video': 'http://localhost:8000',
    },
  },
})
