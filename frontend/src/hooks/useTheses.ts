import { useCallback } from 'react';
import { api } from '../api/client';
import type { ThesisRow } from '../types';
import { useAsyncResource, type AsyncResource } from './useAsyncResource';

/**
 * `GET /api/portfolio/thesis/` — why each open position is held.
 *
 * Plain REST, like attribution: a thesis only changes when a position is opened
 * or closed, which happens inside a Celery run, and the backend does not push it
 * over the WebSocket. Reloads on the dashboard's refresh button.
 */
export function useTheses(token: string | null): AsyncResource<ThesisRow[]> {
  const loader = useCallback((signal: AbortSignal) => api.thesis(signal), []);
  return useAsyncResource<ThesisRow[]>('theses', loader, token !== null);
}
