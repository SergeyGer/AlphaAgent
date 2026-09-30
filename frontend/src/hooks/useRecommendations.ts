import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, unwrapList } from '../api/client';
import type { Recommendation, RecommendationDecisionResponse } from '../types';
import { useAsyncResource } from './useAsyncResource';

export type RecommendationDecision = 'approve' | 'reject';

export interface UseRecommendationsResult {
  recommendations: Recommendation[];
  pending: Recommendation[];
  history: Recommendation[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
  /** IDs with an in-flight approve/reject request. */
  busyIds: ReadonlySet<number>;
  /** Optimistically decides, then reconciles with the server response. */
  decide: (id: number, decision: RecommendationDecision) => Promise<RecommendationDecisionResponse>;
  /** Insert or replace a recommendation (used by WebSocket pushes). */
  upsert: (recommendation: Recommendation) => void;
}

const PENDING: Recommendation['status'] = 'PENDING';

function isPending(recommendation: Recommendation): boolean {
  return recommendation.status === PENDING;
}

function byNewest(a: Recommendation, b: Recommendation): number {
  const left = Date.parse(a.created_at);
  const right = Date.parse(b.created_at);
  if (Number.isNaN(left) || Number.isNaN(right)) return b.id - a.id;
  return right - left;
}

/** `GET /api/portfolio/recommendations/` + approve/reject actions. */
export function useRecommendations(token: string | null): UseRecommendationsResult {
  const loader = useCallback(
    async (signal: AbortSignal) => unwrapList(await api.recommendations(signal)),
    [],
  );
  const resource = useAsyncResource<Recommendation[]>('recommendations', loader, token !== null);
  const { data, setData } = resource;

  const [busyIds, setBusyIds] = useState<ReadonlySet<number>>(() => new Set<number>());

  // Snapshot of the list for rollback when a decision fails (e.g. HTTP 409).
  const dataRef = useRef<Recommendation[] | null>(data);
  useEffect(() => {
    dataRef.current = data;
  }, [data]);

  const upsert = useCallback(
    (recommendation: Recommendation) => {
      setData((previous) => {
        const list = previous ?? [];
        const index = list.findIndex((item) => item.id === recommendation.id);
        if (index === -1) return [recommendation, ...list];
        const next = list.slice();
        next[index] = { ...next[index], ...recommendation };
        return next;
      });
    },
    [setData],
  );

  const decide = useCallback(
    async (id: number, decision: RecommendationDecision) => {
      const rollback = dataRef.current;

      setBusyIds((previous) => {
        const next = new Set(previous);
        next.add(id);
        return next;
      });

      // Optimistic flip — reconciled below with the server's authoritative status.
      const optimisticStatus: Recommendation['status'] =
        decision === 'approve' ? 'APPROVED' : 'REJECTED';
      setData((previous) =>
        previous
          ? previous.map((item) =>
              item.id === id
                ? { ...item, status: optimisticStatus, decided_via: 'USER', decided_at: new Date().toISOString() }
                : item,
            )
          : previous,
      );

      try {
        const response =
          decision === 'approve'
            ? await api.approveRecommendation(id)
            : await api.rejectRecommendation(id);

        setData((previous) =>
          previous
            ? previous.map((item) =>
                item.id === response.id
                  ? { ...item, status: response.status, decided_via: 'USER' }
                  : item,
              )
            : previous,
        );

        return response;
      } catch (error) {
        setData(() => rollback);
        throw error;
      } finally {
        setBusyIds((previous) => {
          const next = new Set(previous);
          next.delete(id);
          return next;
        });
      }
    },
    [setData],
  );

  const recommendations = useMemo(() => (data ?? []).slice().sort(byNewest), [data]);
  const pending = useMemo(() => recommendations.filter(isPending), [recommendations]);
  const history = useMemo(() => recommendations.filter((item) => !isPending(item)), [recommendations]);

  return {
    recommendations,
    pending,
    history,
    loading: resource.loading,
    error: resource.error,
    refresh: resource.refresh,
    busyIds,
    decide,
    upsert,
  };
}
