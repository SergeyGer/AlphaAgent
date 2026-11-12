import type { BadgeTone } from '../components/Badge';
import type {
  PlanApprovalState,
  PlanProvenanceView,
  PlanReview,
  PlanStatus,
  PlanSummary,
} from '../types';

/**
 * Presentation helpers for the advisory-plan review screen (FIX-08).
 *
 * These are deliberately pure and total: an unknown enum value from a newer
 * backend renders as a readable fallback rather than a blank cell, and a
 * missing number renders as `—` rather than `0` (which would look measured).
 */

const STATUS_LABEL: Record<PlanStatus, string> = {
  proposed: 'Proposed',
  applied: 'Applied',
  no_change: 'No change',
  failed: 'Failed',
  superseded: 'Superseded',
  cancelled: 'Cancelled',
  rejected: 'Rejected',
};

const STATUS_TONE: Record<PlanStatus, BadgeTone> = {
  proposed: 'amber',
  applied: 'emerald',
  no_change: 'sky',
  failed: 'rose',
  superseded: 'slate',
  cancelled: 'slate',
  rejected: 'rose',
};

const APPROVAL_LABEL: Record<PlanApprovalState, string> = {
  pending: 'Awaiting approval',
  approved: 'Approved',
  expired: 'Approval expired',
  invalidated: 'Approval invalidated',
  rejected: 'Rejected',
  cancelled: 'Cancelled',
  consumed: 'Approval used',
  not_required: 'No approval needed',
};

const APPROVAL_TONE: Record<PlanApprovalState, BadgeTone> = {
  pending: 'amber',
  approved: 'emerald',
  expired: 'rose',
  invalidated: 'rose',
  rejected: 'rose',
  cancelled: 'slate',
  consumed: 'sky',
  not_required: 'slate',
};

export function planStatusLabel(status: string | null | undefined): string {
  const key = (status ?? '').trim().toLowerCase() as PlanStatus;
  return STATUS_LABEL[key] ?? (key ? key.replace(/_/g, ' ') : 'Unknown');
}

export function planStatusTone(status: string | null | undefined): BadgeTone {
  const key = (status ?? '').trim().toLowerCase() as PlanStatus;
  return STATUS_TONE[key] ?? 'slate';
}

export function approvalStateLabel(state: string | null | undefined): string {
  const key = (state ?? '').trim().toLowerCase() as PlanApprovalState;
  return APPROVAL_LABEL[key] ?? (key ? key.replace(/_/g, ' ') : 'Unknown');
}

export function approvalStateTone(state: string | null | undefined): BadgeTone {
  const key = (state ?? '').trim().toLowerCase() as PlanApprovalState;
  return APPROVAL_TONE[key] ?? 'slate';
}

/** `0.2` → `"20.00%"`; null stays visibly unknown. */
export function formatPlanWeight(value: string | number | null | undefined, decimals = 2): string {
  if (value === null || value === undefined || value === '') return '—';
  const parsed = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(parsed)) return '—';
  return `${(parsed * 100).toFixed(decimals)}%`;
}

/** `-0.3` → `"-30.00%"`, `0` → `"0.00%"` (no misleading sign). */
export function formatWeightDelta(value: string | number | null | undefined, decimals = 2): string {
  if (value === null || value === undefined || value === '') return '—';
  const parsed = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(parsed)) return '—';
  const safe = Math.abs(parsed) < 10 ** -(decimals + 2) ? 0 : parsed;
  const sign = safe > 0 ? '+' : '';
  return `${sign}${(safe * 100).toFixed(decimals)}%`;
}

export function planProviderLabel(
  provenance: Pick<PlanProvenanceView, 'provider' | 'model' | 'synthetic_response'> | null | undefined,
  summary?: Pick<PlanSummary, 'provider' | 'model' | 'synthetic_response'> | null,
): string {
  const provider = (provenance?.provider ?? summary?.provider ?? '').trim();
  const model = (provenance?.model ?? summary?.model ?? '').trim();
  const synthetic = provenance?.synthetic_response ?? summary?.synthetic_response ?? false;
  if (synthetic) return 'Stub (no model call)';
  if (provider && provider !== 'stub' && provider !== 'fixture') {
    return model ? `${provider} / ${model}` : provider;
  }
  return 'Producer not recorded';
}

export function isSyntheticPlan(review: Pick<PlanReview, 'provenance'> | null | undefined): boolean {
  return review?.provenance?.synthetic_response === true;
}

/** A short, reviewer-facing summary of the plan's breadth. */
export function planBreadthLabel(
  weights: { ticker: string; is_cash: boolean; constrained_weight: string | null }[],
): string {
  const risky = weights.filter(
    (row) => !row.is_cash && row.constrained_weight !== null && Number(row.constrained_weight) > 0,
  );
  return `${risky.length} position${risky.length === 1 ? '' : 's'}`;
}

/**
 * ISO expiry for an approval, computed from a duration in hours.
 *
 * The backend enforces its own ceiling; this only builds the explicit value the
 * form submits. It is never used to infer "approved".
 */
export function approvalExpiryIso(hours: number, now: number = Date.now()): string {
  const safeHours = Number.isFinite(hours) ? Math.max(1, Math.min(hours, 168)) : 24;
  return new Date(now + safeHours * 3_600_000).toISOString();
}
