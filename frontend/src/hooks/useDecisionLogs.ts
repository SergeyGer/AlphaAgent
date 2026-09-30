import { useCallback } from 'react';
import { api, unwrapList } from '../api/client';
import type { DecisionLog } from '../types';
import { useAsyncResource, type AsyncResource } from './useAsyncResource';

/** `GET /api/portfolio/logs/` — the persisted AI decision log. */
export function useDecisionLogs(token: string | null): AsyncResource<DecisionLog[]> {
  const loader = useCallback(
    async (signal: AbortSignal) => unwrapList(await api.logs(signal)),
    [],
  );
  return useAsyncResource<DecisionLog[]>('decision-logs', loader, token !== null);
}
