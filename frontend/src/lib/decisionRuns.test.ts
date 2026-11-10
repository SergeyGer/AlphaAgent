import { describe, expect, it } from 'vitest';
import {
  buildTimeline,
  decisionSummary,
  failureHeadline,
  firstSentence,
  formatLatency,
  formatRunTimestamp,
  guardCheckOutcomeLabel,
  isProviderFailure,
  mergeRunEvents,
  partialRunReasons,
  stepReplayCursor,
  eventSequences,
} from './decisionRuns';
import {
  TIMELINE_EVENTS,
  detail,
  evidence,
  run,
  runEvent,
} from '../test/decisionRunFixtures';

describe('decision run formatting', () => {
  it('labels timestamps with an explicit timezone and never fakes an unknown one', () => {
    expect(formatRunTimestamp('2026-11-10T14:09:40Z')).toMatch(/UTC$/);
    expect(formatRunTimestamp(null)).toBe('Unknown');
    expect(formatRunTimestamp('not-a-date')).toBe('Unknown');
  });

  it('renders a null latency as unknown, not as zero milliseconds', () => {
    expect(formatLatency(null)).toBe('Unknown');
    expect(formatLatency(undefined)).toBe('Unknown');
    expect(formatLatency(842)).toBe('842 ms');
    expect(formatLatency(25_000)).toBe('25.0 s');
  });

  it('takes one sentence from a longer rationale', () => {
    expect(firstSentence('First sentence. Second sentence.')).toBe('First sentence.');
    expect(firstSentence('   ')).toBeNull();
    expect(firstSentence(null)).toBeNull();
  });

  it('keeps not_evaluated distinct from fail in the label layer', () => {
    expect(guardCheckOutcomeLabel('pass')).toBe('Pass');
    expect(guardCheckOutcomeLabel('fail')).toBe('Fail');
    expect(guardCheckOutcomeLabel('not_evaluated')).toBe('Not evaluated');
    expect(guardCheckOutcomeLabel('not_evaluated')).not.toBe(guardCheckOutcomeLabel('fail'));
  });

  it('classifies provider failures and budget exhaustion separately', () => {
    expect(isProviderFailure('provider_timeout')).toBe(true);
    expect(isProviderFailure('llm_error')).toBe(true);
    expect(isProviderFailure('database_error')).toBe(false);
    expect(failureHeadline('provider_timeout')).toBe('Provider failure');
    expect(failureHeadline('budget_exhausted')).toContain('budget exhausted');
    expect(failureHeadline(null)).toBe('Run failed');
  });
});

describe('decision run summaries', () => {
  it('uses the stored rationale first sentence when available', () => {
    expect(decisionSummary(detail())).toBe(
      'Rates are falling and data-centre demand is accelerating, so the position is retained with a smaller size.',
    );
  });

  it('states the absence of a summary for each empty case', () => {
    expect(
      decisionSummary(detail({ rationale: null, run: run({ status: 'failed', error_code: 'provider_timeout' }) })),
    ).toContain('Provider failure');
    expect(
      decisionSummary(detail({ rationale: null, run: run({ status: 'analysing', mode: 'live' }) })),
    ).toContain('Analysis is still in progress');
    expect(
      decisionSummary(detail({ rationale: null, run: run({ mode: 'legacy' }) })),
    ).toContain('Legacy decision record');
    expect(decisionSummary(null)).toBe('No decision run selected.');
  });

  it('names missing evidence when the record is partial', () => {
    const reasons = partialRunReasons(
      detail({
        claims: [
          {
            ...detail().claims[0]!,
            evidence_ids: ['ev-missing'],
          },
        ],
        evidence: [evidence()],
        rebuttals: [],
      }),
    );

    expect(reasons.join(' ')).toContain('1 referenced evidence item is not present');
  });

  it('does not call a failed run partial — it has its own state', () => {
    expect(partialRunReasons(detail({ run: run({ status: 'failed', error_code: 'provider_error' }) }))).toEqual([]);
  });
});

describe('event ordering and replay', () => {
  it('merges duplicate event pages by id and keeps sequence order', () => {
    const merged = mergeRunEvents(
      [runEvent({ event_id: 'e2', seq: 2 }), runEvent({ event_id: 'e1', seq: 1 })],
      [runEvent({ event_id: 'e2', seq: 2, payload: { message: 'updated' } })],
    );

    expect(merged.map((event) => event.event_id)).toEqual(['e1', 'e2']);
    expect(merged[1]?.payload['message']).toBe('updated');
  });

  it('steps on the sorted sequence list in both directions', () => {
    const sequences = eventSequences(TIMELINE_EVENTS);
    expect(sequences).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9]);
    expect(stepReplayCursor(TIMELINE_EVENTS, null, 'next')).toBe(1);
    expect(stepReplayCursor(TIMELINE_EVENTS, 1, 'previous')).toBeNull();
    expect(stepReplayCursor(TIMELINE_EVENTS, 4, 'next')).toBe(5);
    expect(stepReplayCursor(TIMELINE_EVENTS, 4, 'previous')).toBe(3);
    expect(stepReplayCursor(TIMELINE_EVENTS, 9, 'next')).toBe(9);
  });

  it('aggregates stage nodes and drops rebuttal stages for legacy runs', () => {
    const nodes = buildTimeline(TIMELINE_EVENTS, true);
    expect(nodes.map((node) => node.stage)).toEqual([
      'collecting',
      'analysing',
      'rebutting',
      'adjudicating',
      'validating',
      'completed',
    ]);
    expect(nodes[0]?.eventCount).toBe(3);

    const legacyNodes = buildTimeline(TIMELINE_EVENTS, false);
    expect(legacyNodes.map((node) => node.stage)).not.toContain('rebutting');
    expect(legacyNodes).toHaveLength(5);
  });

  it('ignores events without a usable sequence instead of collapsing to zero', () => {
    const hostile = [
      runEvent({ event_id: 'ok', seq: 2 }),
      { ...runEvent({ event_id: 'bad' }), seq: Number.NaN },
    ];
    expect(mergeRunEvents([], hostile).map((event) => event.event_id)).toEqual(['ok']);
  });
});
