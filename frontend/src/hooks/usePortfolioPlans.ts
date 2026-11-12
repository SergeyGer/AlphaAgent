import { useCallback } from 'react';
import { api } from '../api/client';
import type { PlanListResponse, PlanReview, PlanSummary } from '../types';
import { useAsyncResource, type AsyncResource } from './useAsyncResource';

/**
 * Advisory-plan data for the FIX-08 review screen.
 *
 * The list and one plan's review are ordinary abortable fetches. Mutations
 * (approve/reject/cancel/revalidate/apply) stay in the screen because each one
 * replaces the whole review payload with the server's authoritative version;
 * optimistically editing a versioned approval would be the one thing this
 * screen must not do.
 */

/** `GET /api/portfolio-plans/` */
export function usePortfolioPlans(token: string | null): AsyncResource<PlanSummary[]> {
  const loader = useCallback(async (signal: AbortSignal): Promise<PlanSummary[]> => {
    const body: PlanListResponse = await api.portfolioPlans(signal);
    return Array.isArray(body?.results) ? body.results : [];
  }, []);

  return useAsyncResource<PlanSummary[]>('portfolio-plans', loader, token !== null);
}

/** `GET /api/portfolio-plans/{id}/` — disabled until a plan is selected. */
export function usePortfolioPlan(
  planId: string | null,
  token: string | null,
): AsyncResource<PlanReview> {
  const loader = useCallback(
    async (signal: AbortSignal): Promise<PlanReview> => {
      if (!planId) throw new Error('No portfolio plan selected.');
      return api.portfolioPlan(planId, signal);
    },
    [planId],
  );

  return useAsyncResource<PlanReview>(
    `portfolio-plan:${planId ?? 'none'}`,
    loader,
    token !== null && planId !== null,
  );
}
