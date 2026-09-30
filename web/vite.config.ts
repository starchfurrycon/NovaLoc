import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// NovaLoc / 新译 — frontend build.
//
// `base: './'` lets the FastAPI backend serve the built assets from any mount point
// (the app uses HashRouter, so no server-side rewrite rules are needed).
//
// `outDir` deliberately points INSIDE the Python package (`src/novaloc/web_dist`).
// Hatchling only ships `src/novaloc`, so putting the build anywhere else means the
// wheel silently ends up without a frontend and `pip install nova-loc` yields a
// server whose UI 404s. Keeping it in the package makes both `pip install` and
// `pip install -e .` work from the same artifacts.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: './',
  build: {
    outDir: '../src/novaloc/web_dist',
    emptyOutDir: true,
    target: 'es2022',
    chunkSizeWarningLimit: 1200,
  },
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      // Dev-only convenience: the backend runs on :8000 by default.
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
})
