import { describe, expect, it } from 'vitest';
import {
  buildAsOfPresentation,
  lastContiguousSequence,
  mergeRunEvents,
  stepReplayCursor,
} from './decisionRuns';
import { detail, guard, run, runEvent } from '../test/decisionRunFixtures';

/**
 * Step 4A — replay must resolve the guard evaluation visible at the cursor.
 *
 * The pre-fix reducer painted `detail.guard` (the final evaluation) from the
 * first `guard.evaluated` event. A run whose preflight approved 10 units and
 * whose final check rejected to 0 therefore showed the rejection at the approval
 * step. These tests pin the identifier join, the legacy-event fallback, the
 * execution cutoff and the forward/backward equivalence on unordered pages.
 */

const preflight = guard({
  id: 'guard-preflight',
  status: 'approved',
  requested_quantity: '10',
  allowed_quantity: '10',
  binding_rule: 'max_trade_allocation_pct',
  quote_time: '2026-11-10T14:10:01Z',
});

const final = guard({
  id: 'guard-final',
  status: 'rejected',
  requested_quantity: '10',
  allowed_quantity: '0',
  binding_rule: 'approval_terms',
  quote_time: '2026-11-10T14:10:04Z',
});

function eventsWithTwoGuards() {
  return [
    runEvent({
      event_id: 's1',
      seq: 1,
      type: 'status.changed',
      stage: 'collecting',
      occurred_at: '2026-11-10T14:09:40Z',
      payload: { status: 'collecting' },
    }),
    runEvent({
      event_id: 's2',
      seq: 2,
      type: 'decision.proposed',
      stage: 'adjudicating',
      occurred_at: '2026-11-10T14:10:00Z',
      payload: { action: 'BUY', decisive_claim_ids: [] },
    }),
    runEvent({
      event_id: 's3',
      seq: 3,
      type: 'guard.evaluated',
      stage: 'validating',
      occurred_at: '2026-11-10T14:10:01Z',
      payload: { guard_id: 'guard-preflight', status: 'approved' },
    }),
    runEvent({
      event_id: 's4',
      seq: 4,
      type: 'guard.evaluated',
      stage: 'validating',
      occurred_at: '2026-11-10T14:10:04Z',
      payload: { guard_id: 'guard-final', status: 'rejected' },
    }),
    runEvent({
      event_id: 's5',
      seq: 5,
      type: 'execution.rejected',
      stage: 'completed',
      occurred_at: '2026-11-10T14:10:05Z',
      payload: { execution_state: 'rejected', no_trade: true },
    }),
    runEvent({
      event_id: 's6',
      seq: 6,
      type: 'outcome.recorded',
      stage: 'completed',
      occurred_at: '2026-11-10T14:10:06Z',
      payload: { outcome_id: 'outcome-30', horizon_days: 30 },
    }),
    runEvent({
      event_id: 's7',
      seq: 7,
      type: 'status.changed',
      stage: 'completed',
      occurred_at: '2026-11-10T14:10:07Z',
      payload: { status: 'completed' },
    }),
  ];
}

function twoGuardDetail() {
  return detail({
    run: run({
      status: 'completed',
      execution_state: 'rejected',
      execution: {
        state: 'rejected',
        executed: false,
        awaiting_approval: false,
        no_trade: true,
      },
    }),
    guard: final,
    guards: [preflight, final],
    guard_evaluations: [preflight, final],
  });
}

describe('guard resolution at the replay cursor', () => {
  it('hides the guard card before the first check', () => {
    const presentation = buildAsOfPresentation(twoGuardDetail(), eventsWithTwoGuards(), 2);
    expect(presentation?.guardVisible).toBe(false);
    expect(presentation?.detail.guard).toBeNull();
  });

  it('shows the preflight approval until the final rejection event is visible', () => {
    const atPreflight = buildAsOfPresentation(twoGuardDetail(), eventsWithTwoGuards(), 3);
    expect(atPreflight?.guardVisible).toBe(true);
    expect(atPreflight?.guardId).toBe('guard-preflight');
    expect(atPreflight?.detail.guard?.status).toBe('approved');
    expect(atPreflight?.detail.guard?.requested_quantity).toBe('10');
    expect(atPreflight?.detail.guard?.allowed_quantity).toBe('10');
    expect(atPreflight?.detail.guard?.binding_rule).toBe('max_trade_allocation_pct');
    expect(atPreflight?.detail.guard?.quote_time).toBe('2026-11-10T14:10:01Z');

    const atFinal = buildAsOfPresentation(twoGuardDetail(), eventsWithTwoGuards(), 4);
    expect(atFinal?.guardId).toBe('guard-final');
    expect(atFinal?.detail.guard?.status).toBe('rejected');
    expect(atFinal?.detail.guard?.allowed_quantity).toBe('0');
  });

  it('shows only what a legacy guard event recorded and marks the rest unavailable', () => {
    const legacyEvents = [
      runEvent({
        event_id: 'l1',
        seq: 1,
        type: 'guard.evaluated',
        stage: 'validating',
        occurred_at: '2026-11-10T14:10:01Z',
        payload: {
          status: 'rejected',
          requested_quantity: '10',
          allowed_quantity: '0',
          binding_rule: 'approval_terms',
        },
      }),
    ];
    const legacyDetail = detail({ guard: final, guards: [], guard_evaluations: [] });
    const presentation = buildAsOfPresentation(legacyDetail, legacyEvents, 1);

    expect(presentation?.guardVisible).toBe(true);
    expect(presentation?.guardPartial).toBe(true);
    expect(presentation?.guardId).toBeNull();
    expect(presentation?.detail.guard?.status).toBe('rejected');
    expect(presentation?.detail.guard?.allowed_quantity).toBe('0');
    expect(presentation?.detail.guard?.checks).toEqual([]);
    expect(presentation?.detail.guard?.unavailable_fields).toContain('checks');
    expect(presentation?.detail.guard?.unavailable_fields).toContain('reason_codes');
  });
});

describe('execution and outcome cutoff', () => {
  it('withholds the execution state until an execution event is visible', () => {
    const before = buildAsOfPresentation(twoGuardDetail(), eventsWithTwoGuards(), 4);
    expect(before?.executionVisible).toBe(false);
    expect(before?.detail.run.execution).toBeNull();
    expect(before?.detail.run.execution_state).toBeNull();

    const after = buildAsOfPresentation(twoGuardDetail(), eventsWithTwoGuards(), 5);
    expect(after?.executionVisible).toBe(true);
    expect(after?.detail.run.execution_state).toBe('rejected');
    expect(after?.detail.run.execution?.no_trade).toBe(true);
  });

  it('applies the same cutoff to outcomes', () => {
    const atExecution = buildAsOfPresentation(twoGuardDetail(), eventsWithTwoGuards(), 5);
    expect(atExecution?.detail.outcomes).toEqual([]);
    const atOutcome = buildAsOfPresentation(twoGuardDetail(), eventsWithTwoGuards(), 6);
    expect(atOutcome?.detail.outcomes.map((outcome) => outcome.horizon_days)).toEqual([30]);
  });
});

describe('paging and bidirectional seeking', () => {
  it('uses the last contiguous event as the known cursor across a gap', () => {
    const pages = [
      runEvent({ event_id: 'p1', seq: 1 }),
      runEvent({ event_id: 'p2', seq: 2 }),
      runEvent({ event_id: 'p5', seq: 5 }),
    ];
    expect(lastContiguousSequence(pages)).toBe(2);
    expect(lastContiguousSequence(mergeRunEvents([], pages))).toBe(2);
  });

  it('merges missing, duplicate and out-of-order pages by event identity', () => {
    const ordered = eventsWithTwoGuards();
    const pageOne = [ordered[4]!, ordered[0]!, ordered[2]!];
    const pageTwo = [ordered[3]!, ordered[1]!, { ...ordered[2]!, payload: { guard_id: 'guard-preflight', status: 'approved' } }, ordered[5]!, ordered[6]!];

    const merged = mergeRunEvents(mergeRunEvents([], pageOne), pageTwo);
    expect(merged.map((event) => event.seq)).toEqual([1, 2, 3, 4, 5, 6, 7]);
    expect(merged.filter((event) => event.event_id === 's3')).toHaveLength(1);

    const atPreflight = buildAsOfPresentation(twoGuardDetail(), merged, 3);
    const atFinal = buildAsOfPresentation(twoGuardDetail(), merged, 4);
    expect(atPreflight?.guardId).toBe('guard-preflight');
    expect(atFinal?.guardId).toBe('guard-final');

    // Seeking backward then forward reproduces the same scene from the same set.
    expect(buildAsOfPresentation(twoGuardDetail(), merged, 3)).toEqual(atPreflight);
    expect(stepReplayCursor(merged, 4, 'previous')).toBe(3);
    expect(stepReplayCursor(merged, 3, 'next')).toBe(4);
    expect(stepReplayCursor(merged, 3, 'previous')).toBe(2);
    expect(stepReplayCursor(merged, 3, 'next')).toBe(4);
  });
});
