import { useCallback } from 'react';
import { api, unwrapList } from '../api/client';
import type { Transaction } from '../types';
import { useAsyncResource, type AsyncResource } from './useAsyncResource';

/** `GET /api/portfolio/transactions/` — newest first, capped for the table. */
export function useTransactions(token: string | null, limit = 25): AsyncResource<Transaction[]> {
  const loader = useCallback(
    async (signal: AbortSignal) => unwrapList(await api.transactions(signal)).slice(0, limit),
    [limit],
  );
  return useAsyncResource<Transaction[]>('transactions', loader, token !== null);
}
