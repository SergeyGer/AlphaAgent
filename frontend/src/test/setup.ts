/**
 * Global test setup.
 *
 * `@testing-library/jest-dom` adds the DOM assertions (`toBeInTheDocument` and
 * friends); `cleanup` after each test keeps a component from one test out of the
 * document of the next, which otherwise produces confusing duplicate-match
 * failures rather than honest ones.
 */
import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

afterEach(() => {
  cleanup();
});

// jsdom implements neither of these, and Recharts' responsive container measures
// its parent on mount. Without a stub the charts render at zero size and the
// component tests fail for a reason that has nothing to do with the component.
if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});
