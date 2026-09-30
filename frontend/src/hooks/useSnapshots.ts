import { useCallback } from 'react';
import { api, unwrapList } from '../api/client';
import type { PortfolioSnapshot, SnapshotRange } from '../types';
import { useAsyncResource, type AsyncResource } from './useAsyncResource';

export const SNAPSHOT_RANGES: readonly SnapshotRange[] = ['24H', '7D', '30D', 'ALL'];

/**
 * `GET /api/portfolio/snapshots/?hours=` — results arrive oldest first and are
 * kept in that order for the equity chart. `ALL` omits the `hours` param.
 */
export function useSnapshots(
  token: string | null,
  range: SnapshotRange,
): AsyncResource<PortfolioSnapshot[]> {
  const loader = useCallback(
    async (signal: AbortSignal) => unwrapList(await api.snapshots(range, signal)),
    [range],
  );
  return useAsyncResource<PortfolioSnapshot[]>(`snapshots:${range}`, loader, token !== null);
}
