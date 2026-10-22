import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      input: {
        // Host app
        main: fileURLToPath(new URL('./index.html', import.meta.url)),
        // Isolated DOCX Viewer V2 document (loaded in an iframe by the host).
        docxViewer: fileURLToPath(new URL('./docx-viewer.html', import.meta.url)),
        // Standalone DTD validator (PXML only — no DOCX/session needed).
        dtdValidator: fileURLToPath(new URL('./dtd-validator.html', import.meta.url)),
      },
    },
  },
  server: {
    port: 4000,
    host: '0.0.0.0',
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        // Don't drop long-running requests (e.g. full-PAX validation of a large doc).
        timeout: 600000,
        proxyTimeout: 600000,
      },
    },
  },
})
