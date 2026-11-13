import { describe, expect, it } from 'vitest';
import {
  buildAsOfPresentation,
  COMPRESSED_STEP_MS,
  decisionRunHref,
  eventsRequiringDetailRefresh,
  isDetailRefreshEvent,
  lastContiguousSequence,
  lastEventSequence,
  missingSequenceCount,
  playbackDelayMs,
  replayBoundary,
  replayTimingLabel,
  runLinkId,
  runStatusAtCursor,
} from './decisionRuns';
import { detail, detailRunEvents, manyRunEvents, outcome, run, runEvent } from '../test/decisionRunFixtures';

/**
 * FIX-04 — replay coherence.
 *
 * The replay scene is a pure function of (detail, events, cursor). These tests
 * pin the properties that acceptance cares about: a cursor before adjudication
 * has no verdict, seeking forward/backward is identical, a gap is recovered from
 * the last contiguous sequence, and recorded-time playback uses event stamps
 * rather than a fixed interval.
 */

const EVENTS = detailRunEvents();

describe('as-of presentation', () => {
  it('hides the verdict, guard and outcomes before adjudication', () => {
    const presentation = buildAsOfPresentation(detail(), EVENTS, 9);
    expect(presentation).not.toBeNull();
    if (!presentation) return;

    expect(presentation.verdictVisible).toBe(false);
    expect(presentation.detail.rationale).toBeNull();
    expect(presentation.detail.action).toBeNull();
    expect(presentation.detail.strongest_objection).toBeNull();
    expect(presentation.detail.decisive_claim_ids).toEqual([]);
    expect(presentation.detail.guard).toBeNull();
    expect(presentation.detail.outcomes).toEqual([]);
    // Evidence and the first six claims had already been persisted.
    expect(presentation.detail.evidence.map((item) => item.id)).toEqual(['ev-1', 'ev-2']);
    expect(presentation.detail.claims).toHaveLength(6);
    expect(presentation.detail.rebuttals).toEqual([]);
    expect(presentation.detail.has_rebuttals).toBe(false);
    expect(presentation.status).toBe('analysing');
  });

  it('reveals the rebuttal, then the verdict, then the guard and outcomes as the cursor advances', () => {
    const atRebuttal = buildAsOfPresentation(detail(), EVENTS, 10);
    expect(atRebuttal?.detail.rebuttals).toHaveLength(1);
    expect(atRebuttal?.verdictVisible).toBe(false);

    const atDecision = buildAsOfPresentation(detail(), EVENTS, 11);
    expect(atDecision?.verdictVisible).toBe(true);
    expect(atDecision?.detail.rationale).toContain('Rates are falling');
    expect(atDecision?.detail.decisive_claim_ids).toEqual(['claim-bull-1']);
    expect(atDecision?.detail.guard).toBeNull();
    expect(atDecision?.detail.outcomes).toEqual([]);
    expect(atDecision?.status).toBe('adjudicating');

    const atGuard = buildAsOfPresentation(detail(), EVENTS, 12);
    expect(atGuard?.guardVisible).toBe(true);
    expect(atGuard?.detail.guard?.status).toBe('clamped');

    const atThirty = buildAsOfPresentation(detail(), EVENTS, 13);
    expect(atThirty?.detail.outcomes.map((outcome) => outcome.horizon_days)).toEqual([30]);

    const atOne = buildAsOfPresentation(detail(), EVENTS, 14);
    expect(atOne?.detail.outcomes.map((outcome) => outcome.horizon_days).sort()).toEqual([1, 30]);

    const atEnd = buildAsOfPresentation(detail(), EVENTS, lastEventSequence(EVENTS));
    expect(atEnd?.atEnd).toBe(true);
    expect(atEnd?.detail.outcomes).toHaveLength(3);
    expect(atEnd?.status).toBe('completed');
  });

  it('yields byte-identical state when the reader seeks backward then forward', () => {
    const forward = buildAsOfPresentation(detail(), EVENTS, 13);
    const back = buildAsOfPresentation(detail(), EVENTS, 10);
    const forwardAgain = buildAsOfPresentation(detail(), EVENTS, 13);

    expect(JSON.stringify(forwardAgain)).toBe(JSON.stringify(forward));
    expect(JSON.stringify(back)).not.toBe(JSON.stringify(forward));
    // The pure function has no internal cursor to drift.
    expect(buildAsOfPresentation(detail(), EVENTS, 10)).toEqual(back);
  });

  it('returns an empty scene at the start and keeps the final summary out of it', () => {
    const presentation = buildAsOfPresentation(detail(), EVENTS, null);
    expect(presentation?.atStart).toBe(true);
    expect(presentation?.detail.claims).toEqual([]);
    expect(presentation?.detail.evidence).toEqual([]);
    expect(presentation?.detail.rationale).toBeNull();
    expect(presentation?.status).toBe('queued');
    expect(presentation?.finalSummary).toBe(
      'Rates are falling and data-centre demand is accelerating, so the position is retained with a smaller size.',
    );
    expect(presentation?.detail.rationale).toBeNull();
  });

  it('uses the final detail when no replay cursor is supplied', () => {
    const presentation = buildAsOfPresentation(detail(), EVENTS, undefined);
    expect(presentation?.atEnd).toBe(true);
    expect(presentation?.detail.claims).toHaveLength(6);
    expect(presentation?.detail.guard).not.toBeNull();
    expect(presentation?.detail.rationale).toContain('Rates are falling');
  });

  it('infers status from persisted transitions without borrowing the final state', () => {
    expect(runStatusAtCursor(EVENTS, null)).toBe('queued');
    expect(runStatusAtCursor(EVENTS, 1)).toBe('collecting');
    expect(runStatusAtCursor(EVENTS, 9)).toBe('analysing');
    expect(runStatusAtCursor(EVENTS, 11)).toBe('adjudicating');
    expect(runStatusAtCursor(EVENTS, 15)).toBe('completed');
  });
});

describe('contiguous recovery', () => {
  it('uses the last unbroken sequence, not the highest one', () => {
    const gap = [
      runEvent({ event_id: 'g1', seq: 1 }),
      runEvent({ event_id: 'g2', seq: 2 }),
      runEvent({ event_id: 'g5', seq: 5 }),
    ];
    expect(lastEventSequence(gap)).toBe(5);
    expect(lastContiguousSequence(gap)).toBe(2);
    expect(missingSequenceCount(gap, 5)).toBe(2);
    expect(lastContiguousSequence([])).toBe(0);
    expect(lastContiguousSequence([runEvent({ event_id: 'x2', seq: 2 })])).toBe(0);
  });

  it('deduplicates by event id and ignores out-of-order page delivery', () => {
    const events = [
      runEvent({ event_id: 'x3', seq: 3 }),
      runEvent({ event_id: 'x1', seq: 1 }),
      runEvent({ event_id: 'x2', seq: 2 }),
      runEvent({ event_id: 'x3', seq: 3, payload: { message: 'newer copy' } }),
    ];
    // mergeRunEvents is covered elsewhere; the contiguous helper is order-safe.
    expect(lastContiguousSequence(events)).toBe(3);
    expect(missingSequenceCount(events, 3)).toBe(0);
  });
});

describe('replay timing', () => {
  it('labels compressed timing explicitly and calculates the fixed delay', () => {
    expect(playbackDelayMs(EVENTS, 10, 1, 'compressed')).toBe(COMPRESSED_STEP_MS);
    expect(playbackDelayMs(EVENTS, 10, 2, 'compressed')).toBe(400);
    expect(playbackDelayMs(EVENTS, 10, 0.5, 'compressed')).toBe(1_600);
    expect(replayTimingLabel('compressed', 1)).toContain('Compressed timing');
    expect(replayTimingLabel('compressed', 1)).toContain('not the recorded pace');
  });

  it('uses stored timestamps for recorded-time playback', () => {
    // d1 14:09:40 → d2 14:09:41 is one second; d2 → d3 is one second.
    expect(playbackDelayMs(EVENTS, 1, 1, 'recorded')).toBe(1_000);
    expect(playbackDelayMs(EVENTS, 1, 2, 'recorded')).toBe(500);
    // d3 14:09:42 → d-claim-0 14:09:50 is eight seconds.
    expect(playbackDelayMs(EVENTS, 3, 1, 'recorded')).toBe(8_000);
    expect(replayTimingLabel('recorded', 1)).toContain('Recorded-time playback');
    expect(replayTimingLabel('recorded', 1)).toContain('not compressed');
  });

  it('falls back to compressed timing when timestamps are unusable', () => {
    const broken = [
      runEvent({ event_id: 'b1', seq: 1, occurred_at: 'not-a-date' }),
      runEvent({ event_id: 'b2', seq: 2, occurred_at: '2026-11-10T14:09:41Z' }),
    ];
    expect(playbackDelayMs(broken, 1, 1, 'recorded')).toBe(COMPRESSED_STEP_MS);
  });
});

describe('detail refresh triggers', () => {
  it('refreshes on persisted stage, decision, guard and outcome events only', () => {
    const token = runEvent({
      event_id: 'token-1',
      seq: 1,
      type: 'agent.token',
      payload: { message: 'streamed text' },
    });
    const claim = runEvent({
      event_id: 'claim-1',
      seq: 2,
      type: 'claim.created',
      payload: { claim_id: 'claim-bull-1' },
    });
    const terminal = runEvent({
      event_id: 'status-1',
      seq: 3,
      type: 'status.changed',
      payload: { status: 'completed' },
    });

    expect(isDetailRefreshEvent(token)).toBe(false);
    expect(isDetailRefreshEvent(claim)).toBe(true);
    expect(isDetailRefreshEvent(terminal)).toBe(true);
    expect(eventsRequiringDetailRefresh([token, claim, terminal], 1).map((event) => event.seq)).toEqual([
      2, 3,
    ]);
  });

  it('covers every persisted event family used by the detail payload', () => {
    const types = [
      'status.changed',
      'stage.started',
      'stage.completed',
      'stage.failed',
      'claim.created',
      'rebuttal.created',
      'decision.proposed',
      'guard.evaluated',
      'outcome.recorded',
      'run.completed',
      'limitation.recorded',
    ];
    for (const type of types) {
      expect(isDetailRefreshEvent(runEvent({ type }))).toBe(true);
    }
  });
});

describe('run links', () => {
  it('reads a mapping from whichever shape the API publishes', () => {
    expect(runLinkId({ run_id: 'run-1' })).toBe('run-1');
    expect(runLinkId({ decision_run_id: 42 })).toBe('42');
    expect(runLinkId({ run: { id: 'nested-run' } })).toBe('nested-run');
    expect(runLinkId({ run_id: '   ' })).toBeNull();
    expect(runLinkId({})).toBeNull();
    expect(runLinkId(null)).toBeNull();
  });

  it('builds an encoded hash route and refuses an empty id', () => {
    expect(decisionRunHref('run-1')).toBe('#/runs/run-1');
    expect(decisionRunHref('run/with space')).toBe('#/runs/run%2Fwith%20space');
    expect(decisionRunHref('')).toBeNull();
    expect(decisionRunHref(null)).toBeNull();
  });
});

describe('large logs', () => {
  it('handles 320 events without a page-count assumption', () => {
    const events = manyRunEvents(320);
    expect(lastContiguousSequence(events)).toBe(320);
    expect(missingSequenceCount(events, 320)).toBe(0);
  });
});


describe('step 4A — outcome replay reads the event revision', () => {
  function revisionEvents() {
    return [
      runEvent({
        event_id: 'v1',
        seq: 1,
        type: 'decision.proposed',
        payload: { action: 'BUY' },
      }),
      runEvent({
        event_id: 'v2',
        seq: 2,
        type: 'outcome.recorded',
        payload: {
          event_schema: 'outcome.recorded.v1',
          outcome_record_id: 'record-7',
          horizon_days: 7,
          method_version: 'attribution.v2',
          maturity: 'evaluated',
          revision: 1,
          measurement_basis: 'execution_fill',
          reference_at: '2026-11-10T14:00:00Z',
          price_observed_at_decision: '2026-11-10T14:00:00Z',
          price_observed_at: '2026-11-17T20:00:00Z',
          benchmark_ticker: 'SPY',
          values: {
            price_at_decision: '100',
            price_at_horizon: '102',
            forward_return_pct: '2.0000',
            decision_return_pct: '2.0000',
            benchmark_return_pct: '1.0000',
          },
        },
      }),
      runEvent({
        event_id: 'v3',
        seq: 3,
        type: 'outcome.recorded',
        payload: {
          event_schema: 'outcome.recorded.v1',
          outcome_record_id: 'record-7',
          horizon_days: 7,
          method_version: 'attribution.v2',
          maturity: 'evaluated',
          revision: 2,
          measurement_basis: 'execution_fill',
          reference_at: '2026-11-10T14:00:00Z',
          price_observed_at_decision: '2026-11-10T14:00:00Z',
          price_observed_at: '2026-11-17T20:00:00Z',
          benchmark_ticker: 'SPY',
          values: {
            price_at_decision: '100',
            price_at_horizon: '104',
            forward_return_pct: '4.0000',
            decision_return_pct: '4.0000',
            benchmark_return_pct: '1.0000',
          },
        },
      }),
    ];
  }

  function revisionDetail() {
    return detail({
      run: run({ status: 'completed' }),
      outcomes: [
        outcome({
          id: 'record-7',
          horizon_days: 7,
          signal_score: '4.0000',
          method_version: 'attribution.v2',
        }),
      ],
      superseded_outcomes: [],
    });
  }

  it('shows 2% before the correction and 4% after it, identically both ways', () => {
    const events = revisionEvents();
    const before = buildAsOfPresentation(revisionDetail(), events, 2);
    expect(before?.detail.outcomes).toHaveLength(1);
    expect(before?.detail.outcomes[0]?.signal_score).toBe('2.0000');
    expect(before?.detail.outcomes[0]?.revision).toBe(1);
    expect(before?.detail.outcomes[0]?.price_at_horizon).toBe('102');
    expect(before?.detail.outcomes[0]?.measurement_basis?.basis).toBe('execution_fill');
    expect(before?.detail.outcomes[0]?.price_observed_at).toBe('2026-11-17T20:00:00Z');

    const after = buildAsOfPresentation(revisionDetail(), events, 3);
    expect(after?.detail.outcomes[0]?.signal_score).toBe('4.0000');
    expect(after?.detail.outcomes[0]?.revision).toBe(2);

    // Seeking backward and forward again in the same session agrees.
    const beforeAgain = buildAsOfPresentation(revisionDetail(), events, 2);
    const afterAgain = buildAsOfPresentation(revisionDetail(), events, 3);
    expect(JSON.stringify(beforeAgain)).toBe(JSON.stringify(before));
    expect(JSON.stringify(afterAgain)).toBe(JSON.stringify(after));
    expect(beforeAgain?.detail.outcomes[0]?.signal_score).toBe('2.0000');
  });

  it('does not borrow the current projection at an earlier cursor', () => {
    const earlier = buildAsOfPresentation(revisionDetail(), revisionEvents(), 1);
    expect(earlier?.detail.outcomes).toEqual([]);
  });
});

describe('step 4B — completeness keeps future detail hidden', () => {
  function body() {
    const events = detailRunEvents();
    return { events, page: detail({ run: run({ status: 'completed' }) }) };
  }

  it('treats a partial page as incomplete when the server reports a later tail', () => {
    const { events, page } = body();
    const partial = buildAsOfPresentation(
      page,
      events.filter((event) => event.seq <= 2),
      2,
      { latestSequence: 15, contiguousSequence: 2 },
    );
    expect(partial?.complete).toBe(false);
    expect(partial?.atEnd).toBe(false);
    expect(partial?.verdictVisible).toBe(false);
    expect(partial?.guardVisible).toBe(false);
    expect(partial?.detail.outcomes).toEqual([]);
    expect(partial?.status).toBe('collecting');
  });

  it('clamps a gap or an out-of-order page to the contiguous sequence', () => {
    const { events, page } = body();
    const held = [events[4]!, events[0]!, events[1]!]; // 5, 1, 2 — a gap at 3, 4
    const boundary = replayBoundary(held, { latestSequence: 15, contiguousSequence: 2 });
    expect(boundary.complete).toBe(false);
    expect(boundary.reachableSequence).toBe(2);

    const scene = buildAsOfPresentation(page, held, 5, {
      latestSequence: 15,
      contiguousSequence: 2,
    });
    expect(scene?.cursorSeq).toBe(2);
    expect(scene?.atEnd).toBe(false);
    expect(scene?.detail.outcomes).toEqual([]);
    expect(scene?.detail.rationale).toBeNull();
  });

  it('reveals the recovered tail only after the gap is filled', () => {
    const { events, page } = body();
    const recovered = buildAsOfPresentation(page, events, lastEventSequence(events), {
      latestSequence: lastEventSequence(events),
      contiguousSequence: lastEventSequence(events),
    });
    expect(recovered?.complete).toBe(true);
    expect(recovered?.atEnd).toBe(true);
    expect(recovered?.detail.outcomes).toHaveLength(3);
  });
});

describe('step 4.3 — status.changed applies payload.to at its sequence', () => {
  it('reads the destination status and not the status it came from', () => {
    const events = [
      runEvent({
        event_id: 't1',
        seq: 1,
        type: 'status.changed',
        payload: { from: 'queued', to: 'collecting' },
      }),
      runEvent({ event_id: 't2', seq: 2, type: 'claim.created', payload: { claim_id: 'c1' } }),
      runEvent({
        event_id: 't3',
        seq: 3,
        type: 'status.changed',
        payload: { from: 'analysing', to: 'adjudicating' },
      }),
      runEvent({
        event_id: 't4',
        seq: 4,
        type: 'status.changed',
        payload: { from: 'adjudicating', to: 'validating' },
      }),
      runEvent({
        event_id: 't5',
        seq: 5,
        type: 'status.changed',
        payload: { from: 'validating', to: 'completed' },
      }),
    ];
    expect(runStatusAtCursor(events, 1)).toBe('collecting');
    expect(runStatusAtCursor(events, 2)).toBe('analysing');
    expect(runStatusAtCursor(events, 3)).toBe('adjudicating');
    expect(runStatusAtCursor(events, 4)).toBe('validating');
    expect(runStatusAtCursor(events, 5)).toBe('completed');
  });

  it('keeps the legacy status field working', () => {
    const events = [
      runEvent({ event_id: 'l1', seq: 1, type: 'status.changed', payload: { status: 'completed' } }),
    ];
    expect(runStatusAtCursor(events, 1)).toBe('completed');
  });
});
