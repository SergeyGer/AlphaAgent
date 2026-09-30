import { useCallback } from 'react';
import { api } from '../api/client';
import type { MarketNewsReport } from '../types';
import { useAsyncResource, type AsyncResource } from './useAsyncResource';

/** Headlines requested per fetch (the backend clamps to 1–30). */
export const NEWS_LIMIT = 10;

/**
 * `GET /api/market/news/` — the same evidence the bull and bear agents argue
 * from, split into positive / negative / neutral coverage.
 *
 * The ticker is uppercased and trimmed so the request key stays stable while
 * the user types.
 */
export function useMarketNews(
  token: string | null,
  ticker: string,
  limit: number = NEWS_LIMIT,
): AsyncResource<MarketNewsReport> {
  const symbol = ticker.trim().toUpperCase();

  const loader = useCallback(
    async (signal: AbortSignal) => api.news(symbol, limit, signal),
    [symbol, limit],
  );

  return useAsyncResource<MarketNewsReport>(
    `market-news:${symbol}:${limit}`,
    loader,
    token !== null && symbol !== '',
  );
}
