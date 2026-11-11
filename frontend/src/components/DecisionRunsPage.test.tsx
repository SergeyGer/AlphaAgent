import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setToken } from '../api/client';
import { AuthProvider } from '../hooks/useAuth';
import type { DecisionRunEventsState } from '../hooks/useDecisionRuns';
import type { AsyncResource } from '../hooks/useAsyncResource';
import { detail, detailRunEvents, run } from '../test/decisionRunFixtures';
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
}));

vi.mock('../hooks/useDecisionRuns', () => ({
  useDecisionRun: () => control.detail,
  useDecisionRunEvents: () => control.events,
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
