import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { AttributionResponse, FeedItem, Transaction } from '../types';
import { DecisionQualityPanel } from './DecisionQualityPanel';
import { ThoughtsFeed } from './ThoughtsFeed';
import { TradesTable } from './TradesTable';

/**
 * FIX-04 — the legacy audit surfaces link to the structured run when a mapping
 * exists, and say nothing when it does not. A link built from a ticker or a
 * timestamp guess would be worse than no link: it would claim that two records
 * explain the same decision when nothing joins them.
 */

const feedItem: FeedItem = {
  key: 'log-77',
  kind: 'decision',
  timestamp: '2026-11-10T14:09:40Z',
  ticker: 'NVDA',
  action: 'BUY 2 NVDA',
  sentiment: 'BULLISH',
  tokensUsed: 100,
  apiCostUsd: '0.00100',
  reasoning: 'Data-centre demand is accelerating.',
  bullCase: null,
  bearCase: null,
  stage: null,
  message: null,
  runId: 'run-20261110-0001',
};

const transaction: Transaction = {
  id: 12,
  ticker: 'NVDA',
  tx_type: 'BUY',
  amount: '2',
  price: '180.00',
  gross_value_usd: '360.00',
  executed_by: 'agent',
  timestamp: '2026-11-10T14:10:06Z',
  run_id: 'run-20261110-0001',
};

const scorecard: AttributionResponse = {
  benchmark: 'SPY',
  horizons: [
    {
      horizon_days: 1,
      evaluated: 1,
      directional_calls: 1,
      hits: 1,
      hit_rate_pct: 100,
      avg_decision_return_pct: '1.0000',
      avg_benchmark_return_pct: '0.1000',
      avg_alpha_pct: '0.9000',
      beat_benchmark: 1,
      benchmarked: 1,
    },
  ],
  recent: [
    {
      id: 1,
      decision_id: 77,
      horizon_days: 1,
      ticker: 'NVDA',
      action: 'BUY',
      price_at_decision: '180.00',
      price_at_horizon: '181.80',
      forward_return_pct: '1.0000',
      decision_return_pct: '1.0000',
      benchmark_return_pct: '0.1000',
      directional_hit: true,
      evaluated_at: '2026-11-11T14:00:00Z',
      run_id: 'run-20261110-0001',
    },
  ],
};

describe('legacy audit → decision run links', () => {
  it('links a thought-feed row with a run mapping', () => {
    render(
      <ThoughtsFeed
        items={[feedItem]}
        loading={false}
        error={null}
        onRetry={vi.fn()}
        cap={100}
      />,
    );

    expect(screen.getByTestId('thought-run-link')).toHaveAttribute(
      'href',
      '#/runs/run-20261110-0001',
    );
  });

  it('does not invent a thought-feed link without a mapping', () => {
    render(
      <ThoughtsFeed
        items={[{ ...feedItem, runId: null }]}
        loading={false}
        error={null}
        onRetry={vi.fn()}
        cap={100}
      />,
    );

    expect(screen.queryByTestId('thought-run-link')).toBeNull();
  });

  it('links a transaction row and marks rows with no mapping', () => {
    const { rerender } = render(
      <TradesTable
        transactions={[transaction]}
        loading={false}
        error={null}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByTestId('transaction-run-link')).toHaveAttribute(
      'href',
      '#/runs/run-20261110-0001',
    );

    rerender(
      <TradesTable
        transactions={[{ ...transaction, run_id: null }]}
        loading={false}
        error={null}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.queryByTestId('transaction-run-link')).toBeNull();
    expect(screen.getByText('No mapping')).toBeInTheDocument();
  });

  it('links a scored outcome row when the scorecard publishes a run id', () => {
    render(
      <DecisionQualityPanel
        scorecard={scorecard}
        loading={false}
        error={null}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByTestId('scorecard-run-link')).toHaveAttribute(
      'href',
      '#/runs/run-20261110-0001',
    );
  });
});
