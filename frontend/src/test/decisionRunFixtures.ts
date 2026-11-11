import type {
  Claim,
  DecisionDetailPayload,
  EvidenceItem,
  GuardCheck,
  GuardComparison,
  OutcomeView,
  Rebuttal,
  RunEvent,
  RunSummary,
} from '../types';

/**
 * Fixtures for the Phase 1 explain screens.
 *
 * They deliberately include awkward-but-legal values: a claim with no
 * evidence, a source with `published_at: null`, a guard check that was never
 * evaluated, and an outcome whose portfolio impact is null.
 */

export function run(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    id: 'run-20261110-0001',
    ticker: 'NVDA',
    status: 'completed',
    mode: 'recorded',
    protocol_version: 'debate.v1',
    started_at: '2026-11-10T14:09:40Z',
    completed_at: '2026-11-10T14:10:05Z',
    latency_ms: 25_000,
    error_code: null,
    ...overrides,
  };
}

export function claim(overrides: Partial<Claim> = {}): Claim {
  return {
    claim_id: 'claim-bull-1',
    agent_role: 'bull',
    stance: 'Constructive',
    statement:
      'Data-centre demand is still growing faster than supply, which supports pricing through the next two quarters.',
    claim_type: 'observed_fact',
    evidence_ids: ['ev-1'],
    horizon: '2 quarters',
    limitations: null,
    ...overrides,
  };
}

export function rebuttal(overrides: Partial<Rebuttal> = {}): Rebuttal {
  return {
    rebuttal_id: 'rebuttal-bear-1',
    responding_agent: 'bear',
    target_claim_id: 'claim-bull-1',
    response_type: 'dispute',
    explanation:
      'The demand figure includes backlog that cannot ship this year, so it overstates near-term revenue.',
    evidence_ids: ['ev-2'],
    position_change: null,
    ...overrides,
  };
}

export function evidence(overrides: Partial<EvidenceItem> = {}): EvidenceItem {
  return {
    id: 'ev-1',
    source_type: 'filing',
    source_uri: 'https://example.com/nvda-10q',
    publisher: 'Example Wire',
    published_at: '2026-11-09T10:00:00Z',
    retrieved_at: '2026-11-10T14:09:41Z',
    excerpt: 'Data-centre revenue rose 94% year over year in the reported quarter.',
    observation: { value: '94', unit: 'percent YoY', currency: null },
    content_hash: 'sha256:2f1c0a9e8d7b6a5c',
    ...overrides,
  };
}

export function missingTimestampEvidence(overrides: Partial<EvidenceItem> = {}): EvidenceItem {
  return evidence({
    id: 'ev-2',
    source_type: 'news',
    source_uri: null,
    publisher: null,
    published_at: null,
    retrieved_at: '2026-11-10T14:09:42Z',
    excerpt: null,
    observation: null,
    content_hash: 'sha256:notimestamp',
    ...overrides,
  });
}

export function guardCheck(overrides: Partial<GuardCheck> = {}): GuardCheck {
  return {
    rule_code: 'MAX_POSITION_PCT',
    outcome: 'pass',
    threshold: '25',
    actual: '18',
    unit: '%',
    ...overrides,
  };
}

export function guard(overrides: Partial<GuardComparison> = {}): GuardComparison {
  return {
    status: 'clamped',
    requested_quantity: '100',
    allowed_quantity: '40',
    binding_rule: 'MAX_POSITION_PCT',
    reason_codes: ['POSITION_LIMIT'],
    checks: [
      guardCheck(),
      guardCheck({ rule_code: 'DAILY_LOSS_LIMIT', outcome: 'fail', threshold: '500', actual: '620', unit: 'USD' }),
      guardCheck({ rule_code: 'LIQUIDITY', outcome: 'not_evaluated', threshold: null, actual: null, unit: null }),
    ],
    ...overrides,
  };
}

export function outcome(overrides: Partial<OutcomeView> = {}): OutcomeView {
  return {
    horizon_days: 30,
    maturity: 'evaluated',
    signal_score: '0.021',
    benchmark: 'SPY',
    portfolio_impact: null,
    method_version: 'attribution.v1',
    ...overrides,
  };
}

const BASE_CLAIMS: Claim[] = [
  claim({
    claim_id: 'claim-bull-1',
    agent_role: 'bull',
    statement:
      'Data-centre demand is still growing faster than supply, which supports pricing through the next two quarters.',
    evidence_ids: ['ev-1', 'ev-2'],
  }),
  claim({
    claim_id: 'claim-bull-2',
    agent_role: 'bull',
    stance: 'Constructive',
    statement: 'Management raised full-year guidance twice in the last four quarters.',
    evidence_ids: ['ev-1'],
  }),
  claim({
    claim_id: 'claim-bull-3',
    agent_role: 'bull',
    stance: 'Neutral',
    statement: 'Gross margin has held above 70% despite the product mix shift.',
    claim_type: 'interpretation',
    evidence_ids: ['ev-1'],
  }),
  claim({
    claim_id: 'claim-bull-4',
    agent_role: 'bull',
    stance: 'Constructive',
    statement: 'The forward order book is the longest it has been in three years.',
    claim_type: 'forecast',
    evidence_ids: [],
  }),
  claim({
    claim_id: 'claim-bear-1',
    agent_role: 'bear',
    stance: 'Cautious',
    statement: 'Customer concentration means one delayed deployment would miss the quarter.',
    claim_type: 'interpretation',
    evidence_ids: ['ev-2'],
    limitations: 'Customer names are redacted in the public filing.',
  }),
  claim({
    claim_id: 'claim-bear-2',
    agent_role: 'bear',
    stance: 'Critical',
    statement: 'Inventory days have risen for three consecutive quarters.',
    claim_type: 'observed_fact',
    evidence_ids: ['ev-1'],
  }),
];

export function detail(overrides: Partial<DecisionDetailPayload> = {}): DecisionDetailPayload {
  return {
    run: run(),
    rationale:
      'Rates are falling and data-centre demand is accelerating, so the position is retained with a smaller size. Competition remains the main unresolved risk.',
    decisive_claim_ids: ['claim-bull-1'],
    strongest_objection:
      'Customer concentration could turn one delayed deployment into a missed quarter, and that objection is not yet resolved.',
    claims: BASE_CLAIMS,
    rebuttals: [rebuttal()],
    evidence: [evidence(), missingTimestampEvidence()],
    guard: guard(),
    outcomes: [
      outcome({
        horizon_days: 1,
        maturity: 'pending',
        signal_score: null,
        benchmark: null,
        portfolio_impact: null,
        method_version: 'attribution.v1',
      }),
      outcome({
        horizon_days: 7,
        maturity: 'matured',
        signal_score: null,
        benchmark: 'SPY',
        portfolio_impact: null,
      }),
      outcome(),
    ],
    has_rebuttals: true,
    ...overrides,
  };
}

export function runEvent(overrides: Partial<RunEvent> = {}): RunEvent {
  return {
    event_id: 'event-1',
    seq: 1,
    type: 'stage.collecting.started',
    stage: 'collecting',
    agent_role: null,
    occurred_at: '2026-11-10T14:09:41Z',
    payload: { message: 'Retrieved 4 sources.' },
    ...overrides,
  };
}

/** Nine events across six stages, including token-level noise in one stage. */
export const TIMELINE_EVENTS: RunEvent[] = [
  runEvent({ event_id: 'e1', seq: 1, stage: 'collecting' }),
  runEvent({
    event_id: 'e2',
    seq: 2,
    stage: 'collecting',
    type: 'agent.token',
    agent_role: 'bull',
    occurred_at: '2026-11-10T14:09:42Z',
    payload: { message: 'Data-centre' },
  }),
  runEvent({
    event_id: 'e3',
    seq: 3,
    stage: 'collecting',
    type: 'agent.token',
    agent_role: 'bear',
    occurred_at: '2026-11-10T14:09:43Z',
    payload: { message: 'demand' },
  }),
  runEvent({
    event_id: 'e4',
    seq: 4,
    stage: 'analysing',
    type: 'claim.created',
    agent_role: 'bull',
    occurred_at: '2026-11-10T14:09:50Z',
    payload: { message: 'Bull claim recorded.' },
  }),
  runEvent({
    event_id: 'e5',
    seq: 5,
    stage: 'analysing',
    type: 'claim.created',
    agent_role: 'bear',
    occurred_at: '2026-11-10T14:09:52Z',
    payload: { message: 'Bear claim recorded.' },
  }),
  runEvent({
    event_id: 'e6',
    seq: 6,
    stage: 'rebutting',
    type: 'rebuttal.created',
    agent_role: 'bear',
    occurred_at: '2026-11-10T14:09:56Z',
    payload: { message: 'Bear disputed the demand claim.' },
  }),
  runEvent({
    event_id: 'e7',
    seq: 7,
    stage: 'adjudicating',
    type: 'stage.adjudicating.finished',
    agent_role: null,
    occurred_at: '2026-11-10T14:10:00Z',
    payload: { message: 'CIO selected the decisive claim.' },
  }),
  runEvent({
    event_id: 'e8',
    seq: 8,
    stage: 'validating',
    type: 'guard.evaluated',
    agent_role: null,
    occurred_at: '2026-11-10T14:10:03Z',
    payload: { message: 'Guard clamped the requested quantity.' },
  }),
  runEvent({
    event_id: 'e9',
    seq: 9,
    stage: 'completed',
    type: 'run.completed',
    agent_role: null,
    occurred_at: '2026-11-10T14:10:05Z',
    payload: { message: 'Run completed.' },
  }),
];

/**
 * An event log whose payloads carry the ids the detail fixture uses.
 *
 * The generic `TIMELINE_EVENTS` above is deliberately token-shaped and carries
 * only display messages, which is enough for the stage aggregator but not for
 * an as-of join. These events let a test move the cursor to before the
 * adjudication and prove the verdict is withheld, then step forward and prove
 * it appears with the same claims and evidence.
 */
export function detailRunEvents(): RunEvent[] {
  const base = [
    runEvent({
      event_id: 'd1',
      seq: 1,
      type: 'status.changed',
      stage: 'collecting',
      occurred_at: '2026-11-10T14:09:40Z',
      payload: { status: 'collecting' },
    }),
    runEvent({
      event_id: 'd2',
      seq: 2,
      type: 'evidence.recorded',
      stage: 'collecting',
      occurred_at: '2026-11-10T14:09:41Z',
      payload: { evidence_id: 'ev-1' },
    }),
    runEvent({
      event_id: 'd3',
      seq: 3,
      type: 'evidence.recorded',
      stage: 'collecting',
      occurred_at: '2026-11-10T14:09:42Z',
      payload: { evidence_id: 'ev-2' },
    }),
  ];
  const claimIds = ['claim-bull-1', 'claim-bull-2', 'claim-bull-3', 'claim-bull-4', 'claim-bear-1', 'claim-bear-2'];
  const claimEvents = claimIds.map((claimId, index) =>
    runEvent({
      event_id: `d-claim-${index}`,
      seq: 4 + index,
      type: 'claim.created',
      stage: 'analysing',
      occurred_at: `2026-11-10T14:09:5${index}Z`,
      payload: { claim_id: claimId },
    }),
  );
  const rest = [
    runEvent({
      event_id: 'd-rebuttal-1',
      seq: 10,
      type: 'rebuttal.created',
      stage: 'rebutting',
      agent_role: 'bear',
      occurred_at: '2026-11-10T14:09:56Z',
      payload: { rebuttal_id: 'rebuttal-bear-1', target_claim_id: 'claim-bull-1' },
    }),
    runEvent({
      event_id: 'd-decision-1',
      seq: 11,
      type: 'decision.proposed',
      stage: 'adjudicating',
      occurred_at: '2026-11-10T14:10:00Z',
      payload: {
        action: 'BUY',
        decisive_claim_ids: ['claim-bull-1'],
        strongest_objection: 'Customer concentration is unresolved.',
      },
    }),
    runEvent({
      event_id: 'd-guard-1',
      seq: 12,
      type: 'guard.evaluated',
      stage: 'validating',
      occurred_at: '2026-11-10T14:10:03Z',
      payload: { guard_id: 'guard-1', status: 'clamped' },
    }),
    runEvent({
      event_id: 'd-outcome-1',
      seq: 13,
      type: 'outcome.recorded',
      stage: 'completed',
      occurred_at: '2026-11-10T14:10:04Z',
      payload: { outcome_id: 'outcome-30', horizon_days: 30 },
    }),
    runEvent({
      event_id: 'd-outcome-2',
      seq: 14,
      type: 'outcome.recorded',
      stage: 'completed',
      occurred_at: '2026-11-10T14:10:05Z',
      payload: { outcome_id: 'outcome-1', horizon_days: 1 },
    }),
    runEvent({
      event_id: 'd-complete',
      seq: 15,
      type: 'status.changed',
      stage: 'completed',
      occurred_at: '2026-11-10T14:10:06Z',
      payload: { status: 'completed' },
    }),
  ];
  return [...base, ...claimEvents, ...rest];
}

/** Three hundred and twenty contiguous events, for pagination tests. */
export function manyRunEvents(count = 320): RunEvent[] {
  return Array.from({ length: count }, (_, index) => {
    const seq = index + 1;
    return runEvent({
      event_id: `many-${seq}`,
      seq,
      type: seq % 10 === 0 ? 'stage.completed' : 'agent.token',
      stage: 'analysing',
      occurred_at: new Date(Date.UTC(2026, 10, 10, 14, 0, 0) + index * 1_000).toISOString(),
      payload: { message: `event ${seq}` },
    });
  });
}
