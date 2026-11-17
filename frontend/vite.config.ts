import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Ports 5173-5175 belong to other local apps, and the backend's CORS allows only 5176.
export default defineConfig({
  plugins: [react()],
  server: { port: 5176, strictPort: true },
})
