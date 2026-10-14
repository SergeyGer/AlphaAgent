import { useCallback } from 'react';
import { api } from '../api/client';
import type { AttributionResponse } from '../types';
import { useAsyncResource, type AsyncResource } from './useAsyncResource';

/**
 * `GET /api/portfolio/attribution/` — was the agent right?
 *
 * Deliberately a plain REST resource: the backend does not push attribution over
 * the WebSocket (outcomes are only written when a horizon elapses), so the panel
 * loads on mount and re-reads on the dashboard's refresh button.
 */
export function useAttribution(token: string | null): AsyncResource<AttributionResponse> {
  const loader = useCallback((signal: AbortSignal) => api.attribution(signal), []);
  return useAsyncResource<AttributionResponse>('attribution', loader, token !== null);
}
