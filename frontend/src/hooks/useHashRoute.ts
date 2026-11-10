import { useEffect, useState } from 'react';

/**
 * Minimal hash router for the Phase 1 explain screen.
 *
 * The existing app is a single authenticated dashboard with no router
 * dependency, so adding one would restructure it. A hash route keeps the
 * dashboard untouched and makes the new screen addressable and linkable:
 *
 *   `#/runs`        → decision-run list
 *   `#/runs/<id>`   → one run's detail + replay
 *   anything else   → the dashboard
 */

export type AppRoute = { view: 'dashboard' } | { view: 'runs'; runId: string | null };

function safeDecode(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

export function parseHashRoute(hash: string): AppRoute {
  const withoutHash = (hash ?? '').replace(/^#/, '');
  const path = (withoutHash.split('?')[0] ?? '').replace(/^\/+/, '');
  const segments = path
    .split('/')
    .filter((segment) => segment !== '')
    .map(safeDecode);

  if (segments[0] === 'runs') {
    return { view: 'runs', runId: segments[1] ?? null };
  }
  return { view: 'dashboard' };
}

export function useHashRoute(): AppRoute {
  const [route, setRoute] = useState<AppRoute>(() =>
    parseHashRoute(typeof window === 'undefined' ? '' : window.location.hash),
  );

  useEffect(() => {
    const handleHashChange = (): void => setRoute(parseHashRoute(window.location.hash));
    window.addEventListener('hashchange', handleHashChange);
    return () => window.removeEventListener('hashchange', handleHashChange);
  }, []);

  return route;
}
