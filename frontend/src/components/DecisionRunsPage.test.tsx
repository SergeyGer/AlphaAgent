import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setToken } from '../api/client';
import { AuthProvider } from '../hooks/useAuth';
import type { DecisionRunEventsState } from '../hooks/useDecisionRuns';
import type { AsyncResource } from '../hooks/useAsyncResource';
import { detail, detailRunEvents, guard, outcome, run, runEvent } from '../test/decisionRunFixtures';
import type { DecisionDetailPayload, RunEvent } from '../types';
import { DecisionRunView } from './DecisionRunsPage';

/**
 * FIX-04 acceptance at the screen level: an open run follows persisted events
 * into a terminal replay without a reload, a cursor before adjudication shows
 * no verdict, and selecting a response highlights its target claim and that
 * claim's evidence.
 *
 * The data hooks are replaced with controllable resources so the test can
 * publish exactly the event sequence the acceptance script describes.
 */

const control = vi.hoisted(() => ({
  detail: null as AsyncResource<DecisionDetailPayload> | null,
  events: null as DecisionRunEventsState | null,
  detailRefresh: vi.fn(),
  eventsRefresh: vi.fn(),
  eventOptions: null as { enabled?: boolean; pollIntervalMs?: number } | null,
}));

vi.mock('../hooks/useDecisionRuns', () => ({
  useDecisionRun: () => control.detail,
  useDecisionRunEvents: (
    _runId: string | null,
    _token: string | null,
    options: { enabled?: boolean; pollIntervalMs?: number } = {},
  ) => {
    control.eventOptions = options;
    return control.events;
  },
  useDecisionRuns: () => ({
    data: [],
    loading: false,
    error: null,
    refresh: vi.fn(),
    setData: vi.fn(),
  }),
}));

function makeDetail(
  overrides: Partial<DecisionDetailPayload> = {},
): AsyncResource<DecisionDetailPayload> {
  return {
    data: detail(overrides),
    loading: false,
    error: null,
    refresh: control.detailRefresh,
    setData: vi.fn(),
  };
}

function makeEvents(events: RunEvent[], tail = events.at(-1)?.seq ?? 0): DecisionRunEventsState {
  return {
    events,
    loading: false,
    error: null,
    lastSequence: tail,
    contiguousSequence: tail,
    serverLastSequence: tail,
    remaining: 0,
    hasMore: false,
    catchingUp: false,
    refresh: control.eventsRefresh,
  };
}

function renderView(runId = 'run-20261110-0001') {
  return render(
    <AuthProvider>
      <DecisionRunView runId={runId} />
    </AuthProvider>,
  );
}

beforeEach(() => {
  setToken('test-token');
  window.history.replaceState(null, '', '#/runs/run-20261110-0001');
  control.detailRefresh.mockClear();
  control.eventsRefresh.mockClear();
});

afterEach(() => {
  setToken(null);
  window.history.replaceState(null, '', '/');
});

describe('DecisionRunView — live refresh', () => {
  it('refreshes detail for persisted events and exposes replay once the run is terminal', async () => {
    const events = detailRunEvents();
    const topNine = events.filter((event) => event.seq <= 9);
    const analysing = makeDetail({
      run: run({ status: 'analysing', completed_at: null, latency_ms: null }),
      rationale: null,
      decisive_claim_ids: [],
      strongest_objection: null,
      guard: null,
      outcomes: [],
      rebuttals: [],
      has_rebuttals: false,
      claims: detail().claims.slice(0, 6),
    });
    control.detail = analysing;
    control.events = makeEvents(topNine, 9);

    const { rerender } = renderView();

    // The run is open while analysing: the detail is visible and replay is not.
    expect(screen.getByTestId('run-status-badge')).toHaveTextContent('Analysing');
    expect(screen.queryByRole('button', { name: 'Play replay' })).toBeNull();
    await waitFor(() => expect(control.detailRefresh).toHaveBeenCalled());

    const publishThrough = async (seq: number): Promise<void> => {
      const before = control.detailRefresh.mock.calls.length;
      control.events = makeEvents(
        events.filter((event) => event.seq <= seq),
        seq,
      );
      rerender(
        <AuthProvider>
          <DecisionRunView runId="run-20261110-0001" />
        </AuthProvider>,
      );
      await waitFor(() => expect(control.detailRefresh.mock.calls.length).toBeGreaterThan(before));
    };

    // Each persisted event family requests a refresh in turn: a rebuttal, the
    // decision, the guard, both outcomes, then the terminal status. The run
    // screen does not wait for a page reload or a manual Refresh click.
    await publishThrough(10);
    await publishThrough(11);
    await publishThrough(12);
    await publishThrough(13);
    await publishThrough(14);
    await publishThrough(15);

    // The refreshed detail is terminal; replay controls appear without reload.
    control.detail = makeDetail();
    rerender(
      <AuthProvider>
        <DecisionRunView runId="run-20261110-0001" />
      </AuthProvider>,
    );

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Play replay' })).toBeInTheDocument(),
    );
    expect(screen.getByTestId('run-status-badge')).toHaveTextContent('Completed');
    expect(screen.getByTestId('timing-mode-label')).toHaveTextContent('Compressed timing');
  });
});

describe('DecisionRunView — replay scene', () => {
  it('contains no verdict before adjudication and reveals it when the decision arrives', async () => {
    window.history.replaceState(
      null,
      '',
      '#/runs/run-20261110-0001?at=9&timing=compressed',
    );
    control.detail = makeDetail();
    control.events = makeEvents(detailRunEvents());

    renderView();
    const user = userEvent.setup();

    // Final summary is pinned outside the scene and labelled as such.
    expect(screen.getByTestId('pinned-final-summary')).toHaveTextContent('Final summary');
    expect(screen.getByTestId('decision-summary')).toHaveTextContent('Replay scene');
    expect(screen.getByTestId('decision-summary')).not.toHaveTextContent('Rates are falling');

    // Cursor 9 is before rebuttal and decision: no verdict, guard or outcomes.
    expect(screen.getByText(/Replay has not reached adjudication/)).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Full CIO rationale' })).toBeNull();
    expect(screen.queryByText('Strongest remaining objection')).toBeNull();
    expect(screen.queryByRole('article', { name: '30-day horizon outcome' })).toBeNull();

    // Step to the rebuttal: it becomes visible, the verdict still does not.
    await user.click(screen.getByRole('button', { name: 'Next event' }));
    expect(
      screen.getByRole('region', { name: 'Targeted responses' }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Replay has not reached adjudication/)).toBeInTheDocument();

    // Selecting the response highlights its actual target claim and evidence.
    await user.click(screen.getByRole('button', { name: /Highlight target claim/ }));
    const targetClaim = screen.getByRole('article', { name: 'Bull claim claim-bull-1' });
    expect(targetClaim).toHaveAttribute('data-highlighted', 'true');
    expect(within(targetClaim).getByText('Target of selected response')).toBeInTheDocument();
    expect(
      within(targetClaim).getByRole('button', { name: /open evidence/i }),
    ).toHaveAttribute('data-highlighted', 'true');

    // Step to the decision: the verdict appears, but the guard is still future.
    await user.click(screen.getByRole('button', { name: 'Next event' }));
    expect(screen.getByRole('region', { name: 'Full CIO rationale' })).toHaveTextContent(
      'Rates are falling',
    );
    expect(screen.getByText('Strongest remaining objection')).toBeInTheDocument();
    expect(screen.getByText(/Replay has not reached validation/)).toBeInTheDocument();
    expect(screen.queryByRole('article', { name: '30-day horizon outcome' })).toBeNull();
  });

  it('preserves the cursor and timing mode in the URL across steps', async () => {
    window.history.replaceState(null, '', '#/runs/run-20261110-0001?at=9&timing=recorded');
    control.detail = makeDetail();
    control.events = makeEvents(detailRunEvents());

    renderView();
    const user = userEvent.setup();

    expect(screen.getByTestId('timing-mode-label')).toHaveTextContent('Recorded-time playback');
    await user.click(screen.getByRole('button', { name: 'Next event' }));

    expect(window.location.hash).toContain('at=10');
    expect(window.location.hash).toContain('timing=recorded');

    await user.click(screen.getByRole('button', { name: 'Compressed timing' }));
    expect(window.location.hash).toContain('at=10');
    expect(window.location.hash).toContain('timing=compressed');
  });
});


describe('DecisionRunView — late outcomes', () => {
  const pendingSchedule = {
    method_version: 'attribution.v2',
    entries: [
      {
        horizon_days: 1,
        state: 'evaluated',
        due_at: null,
        reason: null,
        evaluation_id: 'eval-1',
        measurement_basis: 'execution_fill',
      },
      {
        horizon_days: 7,
        state: 'pending',
        due_at: '2026-11-17T14:10:00Z',
        reason: 'Horizon +7d has not matured.',
        evaluation_id: 'eval-2',
        measurement_basis: 'execution_fill',
      },
    ],
    pending: true,
    next_due_at: '2026-11-17T14:10:00Z',
  };

  it('keeps polling a completed run while an outcome is pending, then stops', async () => {
    const events = detailRunEvents();
    control.detail = makeDetail({
      outcomes: [detail().outcomes[0]!],
      outcome_schedule: pendingSchedule,
    });
    control.events = makeEvents(events);

    const { rerender } = renderView();
    await waitFor(() => expect(control.eventOptions?.pollIntervalMs).toBeGreaterThan(0));

    // The late horizon is now evaluated and the schedule says no work remains.
    const evaluated = detail().outcomes.map((outcome) => ({
      ...outcome,
      maturity: 'evaluated' as const,
      signal_score: '0.02',
    }));
    control.detail = makeDetail({
      outcomes: evaluated,
      outcome_schedule: { ...pendingSchedule, pending: false, next_due_at: null },
    });
    rerender(
      <AuthProvider>
        <DecisionRunView runId="run-20261110-0001" />
      </AuthProvider>,
    );

    await waitFor(() => expect(control.eventOptions?.pollIntervalMs).toBe(0));
  });

  it('refreshes detail when a dropped live notification is recovered on reconnect', async () => {
    const events = detailRunEvents();
    control.detail = makeDetail({
      outcomes: [detail().outcomes[0]!],
      outcome_schedule: pendingSchedule,
    });
    control.events = makeEvents(events);
    const { rerender } = renderView();
    await waitFor(() => expect(control.detailRefresh).toHaveBeenCalled());

    const before = control.detailRefresh.mock.calls.length;
    const lateOutcome = runEvent({
      event_id: 'late-outcome-1',
      seq: 16,
      type: 'outcome.recorded',
      stage: 'completed',
      occurred_at: '2026-11-10T14:10:07Z',
      payload: { outcome_id: 'outcome-late', horizon_days: 7 },
    });
    control.events = makeEvents([...events, lateOutcome], 16);
    // The browser reconnects: the hook resyncs from the contiguous tail and
    // merges the recovered event by identity. The screen refreshes the detail.
    window.dispatchEvent(new Event('online'));
    const evaluated = detail().outcomes.map((outcome) => ({
      ...outcome,
      maturity: 'evaluated' as const,
      signal_score: '0.02',
    }));
    control.detail = makeDetail({
      outcomes: evaluated,
      outcome_schedule: { ...pendingSchedule, pending: false, next_due_at: null },
    });
    rerender(
      <AuthProvider>
        <DecisionRunView runId="run-20261110-0001" />
      </AuthProvider>,
    );

    await waitFor(() =>
      expect(control.detailRefresh.mock.calls.length).toBeGreaterThanOrEqual(before),
    );
    // The recovered event is held once, and the newly evaluated horizon renders
    // once without a manual reload.
    expect(control.events.events.filter((event) => event.event_id === 'late-outcome-1')).toHaveLength(1);
    expect(screen.getAllByRole('article', { name: '7-day horizon outcome' })).toHaveLength(1);
  });

  it('shows an exhausted horizon as unavailable with its reason and a single event', async () => {
    const events = detailRunEvents();
    control.detail = makeDetail({
      outcomes: [detail().outcomes[0]!],
      outcome_schedule: pendingSchedule,
    });
    control.events = makeEvents(events);
    const { rerender } = renderView();
    await waitFor(() => expect(control.detailRefresh).toHaveBeenCalled());

    const lateOutcome = runEvent({
      event_id: 'late-unavailable-1',
      seq: 16,
      type: 'outcome.recorded',
      stage: 'completed',
      occurred_at: '2026-11-10T14:10:07Z',
      payload: {
        event_schema: 'outcome.recorded.v1',
        outcome_record_id: 'record-7',
        horizon_days: 7,
        method_version: 'attribution.v2',
        maturity: 'unavailable',
        revision: 1,
        measurement_basis: 'proposal_close',
        unavailable_reason:
          'no_session_at_or_after_target: No mark exists at or after the target.',
        values: {
          price_at_decision: null,
          price_at_horizon: null,
          forward_return_pct: null,
          decision_return_pct: null,
          benchmark_return_pct: null,
        },
      },
    });
    control.events = makeEvents([...events, lateOutcome], 16);
    window.dispatchEvent(new Event('online'));

    const evaluated = detail().outcomes.map((row) =>
      row.horizon_days === 7
        ? outcome({
            id: 'record-7',
            horizon_days: 7,
            maturity: 'unavailable' as const,
            signal_score: null,
            method_version: 'attribution.v2',
            unavailable_reason:
              'no_session_at_or_after_target: No mark exists at or after the target.',
          })
        : row,
    );
    control.detail = makeDetail({
      outcomes: evaluated,
      outcome_schedule: { ...pendingSchedule, pending: false, next_due_at: null },
    });
    rerender(
      <AuthProvider>
        <DecisionRunView runId="run-20261110-0001" />
      </AuthProvider>,
    );

    await waitFor(() =>
      expect(screen.getByText(/no_session_at_or_after_target/)).toBeInTheDocument(),
    );
    expect(screen.getAllByRole('article', { name: '7-day horizon outcome' })).toHaveLength(1);
    expect(
      control.events.events.filter((event) => event.event_id === 'late-unavailable-1'),
    ).toHaveLength(1);
  });
});


describe('DecisionRunView — authenticated guard replay', () => {
  function twoGuardEvents() {
    return [
      runEvent({
        event_id: 'ag-decision',
        seq: 1,
        type: 'decision.proposed',
        stage: 'adjudicating',
        occurred_at: '2026-11-10T14:10:00Z',
        payload: { action: 'BUY' },
      }),
      runEvent({
        event_id: 'ag-preflight',
        seq: 2,
        type: 'guard.evaluated',
        stage: 'validating',
        occurred_at: '2026-11-10T14:10:01Z',
        payload: { guard_id: 'guard-preflight', status: 'approved' },
      }),
      runEvent({
        event_id: 'ag-final',
        seq: 3,
        type: 'guard.evaluated',
        stage: 'validating',
        occurred_at: '2026-11-10T14:10:04Z',
        payload: { guard_id: 'guard-final', status: 'rejected' },
      }),
      runEvent({
        event_id: 'ag-execution',
        seq: 4,
        type: 'execution.rejected',
        stage: 'completed',
        occurred_at: '2026-11-10T14:10:05Z',
        payload: { execution_state: 'rejected' },
      }),
    ];
  }

  function twoGuardDetail() {
    const preflight = guard({
      id: 'guard-preflight',
      status: 'approved',
      requested_quantity: '10',
      allowed_quantity: '10',
      binding_rule: 'max_trade_allocation_pct',
    });
    const final = guard({
      id: 'guard-final',
      status: 'rejected',
      requested_quantity: '10',
      allowed_quantity: '0',
      binding_rule: 'approval_terms',
    });
    return makeDetail({
      guard: final,
      guards: [preflight, final],
      guard_evaluations: [preflight, final],
      outcomes: [],
    });
  }

  it('shows the approval of 10 at the approval step and the rejection only when visible', async () => {
    window.history.replaceState(null, '', '#/runs/run-20261110-0001?at=2&timing=compressed');
    control.detail = twoGuardDetail();
    control.events = makeEvents(twoGuardEvents());
    renderView();
    const user = userEvent.setup();

    expect(screen.getByTestId('guard-status')).toHaveTextContent('Approved');
    expect(screen.getByText('Requested quantity').nextElementSibling).toHaveTextContent('10');
    expect(screen.getByText('Allowed quantity').nextElementSibling).toHaveTextContent('10');
    expect(screen.queryByText('approval_terms')).toBeNull();

    await user.click(screen.getByRole('button', { name: 'Next event' }));
    expect(screen.getByTestId('guard-status')).toHaveTextContent('Rejected');
    expect(screen.getByText('Allowed quantity').nextElementSibling).toHaveTextContent('0');
    expect(screen.getByText('approval_terms')).toBeInTheDocument();
  });

  it('hides the guard card before the first check', () => {
    window.history.replaceState(null, '', '#/runs/run-20261110-0001?at=1&timing=compressed');
    control.detail = twoGuardDetail();
    control.events = makeEvents(twoGuardEvents());
    renderView();

    expect(screen.queryByTestId('guard-status')).toBeNull();
    expect(screen.getByText(/Replay has not reached validation/)).toBeInTheDocument();
  });
});
