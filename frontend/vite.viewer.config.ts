import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * Build config for the REP-02 offline viewer.
 *
 * Separate from the dashboard config on purpose: the viewer has no API base
 * URL, no WebSocket base URL, no dev proxy and no code path that imports the
 * authenticated client. Its output is a static `docs/viewer/` directory that can
 * be opened through `python -m http.server` or, after the post-build inliner,
 * directly from disk.
 */
export default defineConfig({
  root: fileURLToPath(new URL('./viewer', import.meta.url)),
  base: './',
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: fileURLToPath(new URL('../docs/viewer', import.meta.url)),
    emptyOutDir: true,
    target: 'es2020',
    sourcemap: false,
    // The post-build step inlines the entry chunk and stylesheet. Keeping the
    // asset names stable makes that step a pure text operation.
    assetsInlineLimit: 0,
    rollupOptions: {
      output: {
        entryFileNames: 'assets/[name].js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: 'assets/[name][extname]',
      },
    },
  },
});
