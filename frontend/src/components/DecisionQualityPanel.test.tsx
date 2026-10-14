import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { DecisionQualityPanel, formatHitRate, outcomeOf } from './DecisionQualityPanel';
import type { AttributionResponse, AttributionScorecard, DecisionOutcomeRow } from '../types';

/**
 * The decision-quality panel.
 *
 * This panel exists to answer "was the agent right?", which makes two rendering
 * mistakes worse than a crash:
 *
 * * showing `0%` where the backend said `null` — reporting a total failure that
 *   was never measured, and
 * * folding a HOLD (`directional_hit: null`) in with the misses — punishing the
 *   system for declining to make a call.
 *
 * Both are covered here, along with the empty and hostile payloads that turn a
 * scorecard into `NaN` on screen.
 */

const horizon = (overrides: Partial<AttributionScorecard> = {}): AttributionScorecard => ({
  horizon_days: 1,
  evaluated: 15,
  directional_calls: 15,
  hits: 4,
  hit_rate_pct: 26.7,
  avg_decision_return_pct: '-0.2666',
  avg_benchmark_return_pct: '-0.0463',
  avg_alpha_pct: '-0.2203',
  beat_benchmark: 3,
  benchmarked: 15,
  ...overrides,
});

const outcome = (overrides: Partial<DecisionOutcomeRow> = {}): DecisionOutcomeRow => ({
  id: 1,
  decision_id: 1490,
  horizon_days: 1,
  ticker: 'AAPL',
  action: 'SELL',
  price_at_decision: '332.89000000',
  price_at_horizon: '336.70000000',
  forward_return_pct: '1.1457',
  decision_return_pct: '-1.1457',
  benchmark_return_pct: '0.5995',
  directional_hit: false,
  evaluated_at: '2026-10-14T13:00:00Z',
  ...overrides,
});

/** Mirrors the live payload, including the unflattering 1d hit rate. */
const scorecard = (overrides: Partial<AttributionResponse> = {}): AttributionResponse => ({
  benchmark: 'SPY',
  horizons: [
    horizon(),
    horizon({
      horizon_days: 7,
      evaluated: 2,
      directional_calls: 2,
      hits: 0,
      hit_rate_pct: 0.0,
      avg_decision_return_pct: '-1.4768',
      avg_benchmark_return_pct: '-0.2162',
      avg_alpha_pct: '-1.2606',
      beat_benchmark: 0,
      benchmarked: 2,
    }),
    horizon({
      horizon_days: 30,
      evaluated: 0,
      directional_calls: 0,
      hits: 0,
      hit_rate_pct: null,
      avg_decision_return_pct: '0.0000',
      avg_benchmark_return_pct: null,
      avg_alpha_pct: null,
      beat_benchmark: 0,
      benchmarked: 0,
    }),
  ],
  recent: [
    outcome(),
    outcome({ id: 2, decision_id: 1200, ticker: 'BTC', action: 'BUY', directional_hit: true, decision_return_pct: '1.0652' }),
    outcome({ id: 3, decision_id: 1100, ticker: 'MSFT', action: 'HOLD', directional_hit: null, decision_return_pct: '0.0000' }),
  ],
  ...overrides,
});

function renderPanel(overrides: Partial<AttributionResponse> = {}): ReturnType<typeof render> {
  return render(
    <DecisionQualityPanel
      scorecard={scorecard(overrides)}
      loading={false}
      error={null}
      onRetry={vi.fn()}
    />,
  );
}

describe('DecisionQualityPanel', () => {
  it('renders the hit rate, averages, alpha and scored count per horizon', () => {
    renderPanel();

    expect(screen.getByText('Decision Quality')).toBeInTheDocument();
    expect(screen.getByText('vs SPY · 3 recent scored decisions')).toBeInTheDocument();

    const oneDay = screen.getByRole('region', { name: '1d horizon scorecard' });
    expect(within(oneDay).getByText('26.7%')).toBeInTheDocument();
    expect(within(oneDay).getByText('4 of 15 directional calls')).toBeInTheDocument();
    expect(within(oneDay).getByText('15 scored')).toBeInTheDocument();
    expect(within(oneDay).getByText('-0.27%')).toBeInTheDocument();
    expect(within(oneDay).getByText('-0.22%')).toBeInTheDocument();
    expect(within(oneDay).getByText('3 of 15')).toBeInTheDocument();
  });

  it('renders a null hit rate as an em dash, never as 0%', () => {
    renderPanel();

    const thirtyDay = screen.getByRole('region', { name: '30d horizon scorecard' });

    // The regression this guards: an unscored horizon reading "0.0%" reports a
    // total failure that was never measured.
    const hitRateValue = within(thirtyDay).getByText('hit rate').previousElementSibling;
    expect(hitRateValue).toHaveTextContent('—');
    expect(within(thirtyDay).queryByText('0.0%')).toBeNull();
    expect(within(thirtyDay).getByText('nothing scored yet')).toBeInTheDocument();
  });

  it('does not round a measured zero up into something better-looking', () => {
    renderPanel();

    const sevenDay = screen.getByRole('region', { name: '7d horizon scorecard' });

    // 0.0% here is a real measurement (0 of 2 calls), not missing data, so it is
    // shown as it is — only the null horizon gets an em dash.
    expect(within(sevenDay).getByText('0.0%')).toBeInTheDocument();
    expect(within(sevenDay).getByText('0 of 2 directional calls')).toBeInTheDocument();
    expect(screen.getAllByText('0.0%')).toHaveLength(1);
  });

  it('distinguishes a decision that made no call from a miss', () => {
    renderPanel();

    expect(screen.getAllByText('Miss')).toHaveLength(1);
    expect(screen.getAllByText('Hit')).toHaveLength(1);

    const holdRow = screen.getByText('MSFT').closest('li');
    expect(holdRow).not.toBeNull();
    expect(within(holdRow as HTMLElement).getByText('No call')).toBeInTheDocument();
    expect(within(holdRow as HTMLElement).queryByText('Miss')).toBeNull();

    // The null-hit decision still shows what it was worth (zero for a HOLD).
    expect(within(holdRow as HTMLElement).getByText('0.00%')).toBeInTheDocument();
  });

  it('renders the scorecard with an empty recent list', () => {
    renderPanel({ recent: [] });

    expect(screen.getByText('Nothing scored yet')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: '1d horizon scorecard' })).toBeInTheDocument();
  });

  it('renders all-null horizons without inventing numbers', () => {
    const empty = {
      evaluated: 0,
      directional_calls: 0,
      hits: 0,
      hit_rate_pct: null,
      avg_decision_return_pct: null,
      avg_benchmark_return_pct: null,
      avg_alpha_pct: null,
      beat_benchmark: 0,
      benchmarked: 0,
    };

    const { container } = renderPanel({
      horizons: [
        horizon({ ...empty, horizon_days: 1 }),
        horizon({ ...empty, horizon_days: 7 }),
        horizon({ ...empty, horizon_days: 30 }),
      ],
      recent: [],
    });

    expect(screen.getAllByRole('region', { name: /horizon scorecard/ })).toHaveLength(3);
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(3);
    expect(container.textContent).not.toMatch(/NaN|undefined|Infinity/);
    expect(container.textContent).not.toMatch(/0\.0%/);
  });

  it('renders a hostile payload with missing fields without NaN', () => {
    const hostile = {
      benchmark: undefined,
      horizons: [
        { horizon_days: undefined, evaluated: undefined, hit_rate_pct: undefined },
        { horizon_days: 'nonsense', hit_rate_pct: 'NaN', avg_alpha_pct: '' },
      ],
      recent: [
        { id: undefined, ticker: undefined, action: undefined, directional_hit: undefined },
        { id: 9, ticker: 'AAPL', action: null, decision_return_pct: 'garbage', directional_hit: 'yes' },
      ],
    } as unknown as AttributionResponse;

    const { container } = render(
      <DecisionQualityPanel scorecard={hostile} loading={false} error={null} onRetry={vi.fn()} />,
    );

    expect(container.textContent).not.toMatch(/NaN|undefined|Infinity/);
    // An unrecognised `directional_hit` is not silently counted as a hit or a miss.
    expect(screen.getAllByText('No call').length).toBeGreaterThanOrEqual(1);
  });

  it('shows the error with a retry when no scorecard has loaded', () => {
    const onRetry = vi.fn();
    render(
      <DecisionQualityPanel scorecard={null} loading={false} error="Network error" onRetry={onRetry} />,
    );

    expect(screen.getByRole('alert')).toHaveTextContent('Network error');
    screen.getByRole('button', { name: /retry/i }).click();
    expect(onRetry).toHaveBeenCalled();
  });
});

describe('decision-quality helpers', () => {
  it('formats a null hit rate as an em dash and a real one as a percentage', () => {
    expect(formatHitRate(null)).toBe('—');
    expect(formatHitRate(undefined)).toBe('—');
    expect(formatHitRate('nonsense')).toBe('—');
    expect(formatHitRate(26.7)).toBe('26.7%');
    expect(formatHitRate(0)).toBe('0.0%');
  });

  it('classifies only explicit booleans as a hit or a miss', () => {
    expect(outcomeOf({ directional_hit: true })).toBe('hit');
    expect(outcomeOf({ directional_hit: false })).toBe('miss');
    expect(outcomeOf({ directional_hit: null })).toBe('not-a-call');
    expect(outcomeOf({} as never)).toBe('not-a-call');
  });
});
