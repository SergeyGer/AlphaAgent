import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

/**
 * Vitest configuration for the dashboard.
 *
 * Two ideas shape what is tested here. First, the pure modules — the formatters,
 * the wire-frame validator, the feed merge — carry most of the frontend's real
 * logic and none of its rendering, so they are cheap to test exhaustively.
 * Second, the few components worth rendering are the ones where a silent failure
 * blanks the screen rather than throwing, because those are the failures a
 * screenshot misses and a user reports as "it's broken".
 *
 * `jsdom` rather than a real browser: these run on every push and should take
 * seconds. The end-to-end smoke test that needs a real browser lives in
 * `e2e/` and runs against the actual stack.
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    // Playwright specs live in e2e/ and must not be picked up here; they need a
    // running server and would fail confusingly.
    exclude: ['node_modules/**', 'dist/**', 'e2e/**'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json-summary'],
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/**/*.{test,spec}.{ts,tsx}', 'src/test/**', 'src/main.tsx', 'src/vite-env.d.ts'],
      // Two floors rather than one, because a single number would be misleading
      // in both directions. The pure logic under src/lib carries the frontend's
      // real complexity and is expected to stay well covered; the components and
      // hooks are covered only where a silent failure would blank a panel, so a
      // 75% floor there would be theatre. The global floor is a ratchet set just
      // below today's figure: it cannot fall, and it rises as coverage does.
      thresholds: {
        'src/lib/**': { statements: 75, branches: 70, functions: 70, lines: 75 },
        'src/**': { statements: 15, branches: 18, functions: 10, lines: 14 },
      },
    },
  },
});
