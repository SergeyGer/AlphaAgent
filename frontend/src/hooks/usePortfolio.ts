import { useCallback } from 'react';
import { api } from '../api/client';
import type { Portfolio } from '../types';
import { useAsyncResource, type AsyncResource } from './useAsyncResource';

/** `GET /api/portfolio/` — the dashboard's root resource. */
export function usePortfolio(token: string | null): AsyncResource<Portfolio> {
  const loader = useCallback((signal: AbortSignal) => api.portfolio(signal), []);
  return useAsyncResource<Portfolio>('portfolio', loader, token !== null);
}
