import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * AlphaAgent dashboard build config.
 *
 * REST calls use `VITE_API_BASE` (empty by default → same-origin relative URLs).
 * In development both `/api` and `/ws` are proxied to the Django backend.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: true,
    port: 5173,
    strictPort: false,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      output: {
        // Split third-party code (React, Recharts, d3) from app code so the
        // vendor bundle can be cached across deploys.
        manualChunks(id: string) {
          return id.includes('node_modules') ? 'vendor' : undefined;
        },
      },
    },
  },
});
