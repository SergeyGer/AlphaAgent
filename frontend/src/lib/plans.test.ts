import { describe, expect, it } from 'vitest';
import {
  approvalExpiryIso,
  approvalStateLabel,
  approvalStateTone,
  formatPlanWeight,
  formatWeightDelta,
  isSyntheticPlan,
  planProviderLabel,
  planStatusLabel,
  planStatusTone,
} from './plans';

describe('plan presentation helpers', () => {
  it('labels known and unknown plan statuses without blank output', () => {
    expect(planStatusLabel('proposed')).toBe('Proposed');
    expect(planStatusLabel('no_change')).toBe('No change');
    expect(planStatusLabel('something_new')).toBe('something new');
    expect(planStatusTone('rejected')).toBe('rose');
    expect(planStatusTone('unknown')).toBe('slate');
  });

  it('distinguishes approval states', () => {
    expect(approvalStateLabel('approved')).toBe('Approved');
    expect(approvalStateLabel('invalidated')).toBe('Approval invalidated');
    expect(approvalStateTone('expired')).toBe('rose');
    expect(approvalStateTone('pending')).toBe('amber');
  });

  it('formats weights and changes as percentages, preserving unknown', () => {
    expect(formatPlanWeight('0.2')).toBe('20.00%');
    expect(formatPlanWeight(null)).toBe('—');
    expect(formatWeightDelta('-0.3')).toBe('-30.00%');
    expect(formatWeightDelta('0')).toBe('0.00%');
    expect(formatWeightDelta(null)).toBe('—');
  });

  it('never presents a stub as a model decision', () => {
    expect(
      planProviderLabel({ provider: 'anthropic', model: 'claude-haiku-5-5', synthetic_response: false }),
    ).toBe('anthropic / claude-haiku-5-5');
    expect(
      planProviderLabel({ provider: 'stub', model: 'stub-model', synthetic_response: true }),
    ).toBe('Stub (no model call)');
    expect(isSyntheticPlan({ provenance: { synthetic_response: true } } as never)).toBe(true);
    expect(isSyntheticPlan(null)).toBe(false);
  });

  it('builds an explicit expiry inside the backend ceiling', () => {
    const now = Date.parse('2026-01-01T00:00:00Z');
    expect(approvalExpiryIso(1, now)).toBe('2026-01-01T01:00:00.000Z');
    expect(approvalExpiryIso(999, now)).toBe('2026-01-08T00:00:00.000Z');
  });
});
