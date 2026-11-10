import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DecisionDetail } from './DecisionDetail';
import { detail, run } from '../test/decisionRunFixtures';
import type { DecisionDetailPayload, RunMode } from '../types';

/**
 * UX-01 — the primary explain screen.
 *
 * These tests pin the states that are easiest to get quietly wrong: a mode
 * badge that disappears into colour, prose that expands without limit, an
 * evidence drawer that secretly refetches, a rebuttal section invented for a
 * pipeline that had none, and a pending outcome rendered as a measured zero.
 */

function renderDetail(overrides: Partial<DecisionDetailPayload> = {}): ReturnType<typeof render> {
  return render(
    <DecisionDetail
      detail={detail(overrides)}
      loading={false}
      error={null}
      onRetry={vi.fn()}
    />,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('DecisionDetail — provenance header', () => {
  const MODES: ReadonlyArray<readonly [RunMode, string]> = [
    ['live', 'Live'],
    ['recorded', 'Recorded'],
    ['heuristic', 'Heuristic'],
    ['legacy', 'Legacy'],
    ['synthetic', 'Synthetic'],
  ];

  it.each(MODES)('renders the %s mode badge as "%s"', (mode, label) => {
    renderDetail({ run: run({ mode }) });

    const badge = screen.getByTestId('run-mode-badge');
    expect(within(badge).getByText(label)).toBeInTheDocument();
  });

  it('shows the actual run status and a timezone-labelled timestamp', () => {
    renderDetail({ run: run({ status: 'validating' }) });

    expect(within(screen.getByTestId('run-status-badge')).getByText('Validating')).toBeInTheDocument();
    // A bare clock time would be ambiguous; UTC is part of the rendered value.
    expect(screen.getByText(/Started/).closest('div')?.textContent).toMatch(/UTC/);
  });

  it('says the start time is unknown instead of rendering a placeholder date', () => {
    renderDetail({ run: run({ started_at: null }) });

    expect(screen.getByText('Start time unknown')).toBeInTheDocument();
    expect(screen.queryByText(/1970/)).toBeNull();
  });

  it('shows a provider failure as a provider failure, with the error code', () => {
    const { container } = renderDetail({
      run: run({ status: 'failed', mode: 'live', error_code: 'provider_timeout', completed_at: null }),
    });

    const notice = container.querySelector('[data-state="provider-failure"]');
    expect(notice).not.toBeNull();
    expect(notice).toHaveTextContent('Provider failure');
    expect(notice).toHaveTextContent('provider_timeout');
    expect(within(screen.getByTestId('run-status-badge')).getByText('Failed')).toBeInTheDocument();
  });
});

describe('DecisionDetail — summary and states', () => {
  it('renders exactly the first sentence of the rationale as the summary', () => {
    renderDetail();

    expect(screen.getByTestId('decision-summary')).toHaveTextContent(
      'Rates are falling and data-centre demand is accelerating, so the position is retained with a smaller size.',
    );
    expect(screen.getByTestId('decision-summary')).not.toHaveTextContent(
      'Competition remains the main unresolved risk.',
    );
  });

  it('shows pending analysis as its own state, not as an empty completed run', () => {
    const { container } = renderDetail({
      run: run({ status: 'analysing', mode: 'live', completed_at: null }),
      claims: [],
      rebuttals: [],
      has_rebuttals: false,
    });

    const notice = container.querySelector('[data-state="analysis-in-progress"]');
    expect(notice).not.toBeNull();
    expect(notice).toHaveTextContent('Analysis in progress');
    expect(screen.getByText(/No bull claims yet/)).toBeInTheDocument();
  });

  it('labels a heuristic run as a heuristic fallback', () => {
    const { container } = renderDetail({
      run: run({ mode: 'heuristic' }),
      has_rebuttals: false,
      rebuttals: [],
    });

    const notice = container.querySelector('[data-state="heuristic-fallback"]');
    expect(notice).toHaveTextContent('Heuristic fallback');
    expect(within(screen.getByTestId('run-mode-badge')).getByText('Heuristic')).toBeInTheDocument();
  });

  it('labels legacy data and does not invent claims for it', () => {
    const { container } = renderDetail({
      run: run({ mode: 'legacy' }),
      claims: [],
      rebuttals: [],
      has_rebuttals: false,
    });

    expect(container.querySelector('[data-state="legacy-data"]')).toHaveTextContent('Legacy report');
    expect(
      screen.getAllByText('Legacy report — individual claims were not recorded in this format.'),
    ).toHaveLength(2);
  });

  it('renders a partial record with each missing component named', () => {
    const { container } = renderDetail({ guard: null, rationale: null });

    const notice = container.querySelector('[data-state="partial-run"]');
    expect(notice).not.toBeNull();
    expect(notice).toHaveTextContent('No CIO rationale was recorded.');
    expect(notice).toHaveTextContent('No guard comparison was recorded.');
  });

  it('marks missing evidence without substituting anything', () => {
    const { container } = renderDetail({ evidence: [] });

    expect(container.querySelectorAll('[data-state="missing-evidence"]').length).toBeGreaterThan(0);
    expect(screen.getAllByText(/missing from this record/i).length).toBeGreaterThan(0);
  });
});

describe('DecisionDetail — claims and evidence', () => {
  it('shows at most three claims per side, then expands on request', async () => {
    const user = userEvent.setup();
    renderDetail();

    expect(screen.queryByText('The forward order book is the longest it has been in three years.')).toBeNull();
    expect(screen.getByText('Inventory days have risen for three consecutive quarters.')).toBeInTheDocument();

    const expand = screen.getByRole('button', { name: 'Show all 4 bull claims' });
    expect(expand).toHaveAttribute('aria-expanded', 'false');
    await user.click(expand);

    expect(
      screen.getByText('The forward order book is the longest it has been in three years.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Show fewer bull claims' })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
  });

  it('opens the evidence drawer from a claim without refetching', async () => {
    const user = userEvent.setup();
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    renderDetail();

    const opener = screen.getAllByRole('button', { name: /open evidence/i })[0];
    expect(opener).toBeDefined();
    await user.click(opener as HTMLElement);

    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByRole('heading', { name: 'Stored evidence' })).toBeInTheDocument();
    // The drawer reads the already-loaded evidence list; opening it is not a request.
    expect(fetchSpy).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Close evidence drawer' }));
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});

describe('DecisionDetail — targeted responses and guard', () => {
  it('renders no targeted-response section at all when has_rebuttals is false', () => {
    renderDetail({ has_rebuttals: false, rebuttals: [] });

    expect(screen.queryByText('Targeted responses')).toBeNull();
    expect(screen.queryByRole('region', { name: 'Targeted responses' })).toBeNull();
    expect(
      screen.queryByText(
        'The demand figure includes backlog that cannot ship this year, so it overstates near-term revenue.',
      ),
    ).toBeNull();
  });

  it('renders the targeted response when the protocol includes them', () => {
    renderDetail();

    const region = screen.getByRole('region', { name: 'Targeted responses' });
    expect(within(region).getByText(/Bear responds/)).toBeInTheDocument();
    expect(within(region).getByText('Disputes claim')).toBeInTheDocument();
    expect(
      within(region).getByText(
        'The demand figure includes backlog that cannot ship this year, so it overstates near-term revenue.',
      ),
    ).toBeInTheDocument();
  });

  it('shows decisive evidence, the strongest objection and the rationale', () => {
    renderDetail();

    expect(screen.getByText('Decisive evidence')).toBeInTheDocument();
    expect(screen.getByText('Strongest remaining objection')).toBeInTheDocument();
    expect(screen.getByText(/Customer concentration could turn one delayed deployment/)).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Full CIO rationale' })).toHaveTextContent(
      'Rates are falling and data-centre demand is accelerating',
    );
    // The contract has no separate action field; the screen says so instead of guessing.
    expect(screen.getByText(/does not store a separate action field/)).toBeInTheDocument();
  });

  it('renders outcomes without turning a null portfolio impact into zero', () => {
    renderDetail();

    const pending = screen.getByRole('article', { name: '1-day horizon outcome' });
    expect(within(pending).getByText('Not yet matured')).toBeInTheDocument();
    expect(within(pending).getByText(/no measurement exists/)).toBeInTheDocument();
    expect(within(pending).queryByText(/Signal score/)).toBeNull();

    const evaluated = screen.getByRole('article', { name: '30-day horizon outcome' });
    expect(within(evaluated).getByText('0.021')).toBeInTheDocument();
    expect(within(evaluated).getByText('SPY')).toBeInTheDocument();
    expect(within(evaluated).getByText('Not available')).toBeInTheDocument();
    expect(within(evaluated).queryByText('0.00')).toBeNull();

    const matured = screen.getByRole('article', { name: '7-day horizon outcome' });
    expect(within(matured).getByText('Matured — evaluation pending')).toBeInTheDocument();
  });
});
