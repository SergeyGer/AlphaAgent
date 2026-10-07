import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MetricsGrid } from './MetricsGrid';
import type { Portfolio } from '../types';

/**
 * The metric grid.
 *
 * Chosen for rendering tests over the other components because its failure mode
 * is the one a screenshot cannot catch and a user cannot describe: given a metric
 * that is `undefined`, it used to render `NaN` across the cards, and given a
 * payload with no `metrics` at all it blanked entirely. Both look like "the
 * dashboard is broken" rather than like one missing field.
 *
 * The spending meter is covered here too, since it is the newest card and the
 * easiest to break: a missing `ai_budget` must not take the other six down with
 * it.
 */

const basePortfolio = (overrides: Partial<Portfolio> = {}): Portfolio =>
  ({
    id: 1,
    username: 'demo',
    balance_usd: '21801.49',
    risk_profile: 'high',
    risk_profile_display: 'Aggressive',
    is_autonomous: true,
    max_trade_allocation_pct: '5.00',
    max_trade_budget_usd: '1090.07',
    daily_loss_limit_usd: '500.00',
    created_at: '2026-09-30T00:00:00Z',
    assets: [],
    metrics: {
      total_equity_usd: '26542.11',
      cash_balance_usd: '21801.49',
      positions_value_usd: '4740.62',
      invested_cost_usd: '4680.57',
      unrealised_pnl_usd: '60.05',
      unrealised_pnl_pct: '1.28',
      realised_pnl_today_usd: '0.00',
      realised_pnl_total_usd: '1482.05',
      daily_loss_limit_usd: '500.00',
      daily_loss_used_usd: '0.00',
      daily_loss_remaining_usd: '500.00',
      unpriced_tickers: [],
      is_autonomy_blocked: false,
      block_reason: null,
    },
    ai_budget: {
      spent_usd: '1.75',
      limit_usd: '5.00',
      remaining_usd: '3.25',
      used_pct: 35,
      enforced: true,
      exhausted: false,
    },
    ...overrides,
  }) as unknown as Portfolio;

describe('MetricsGrid', () => {
  it('renders the headline metrics from the payload', () => {
    render(<MetricsGrid portfolio={basePortfolio()} loading={false} />);

    expect(screen.getByText('Total Equity')).toBeInTheDocument();
    expect(screen.getByText('$26,542.11')).toBeInTheDocument();
    expect(screen.getByText('$21,801.49')).toBeInTheDocument();
  });

  it('shows the AI spend meter with its remaining budget', () => {
    render(<MetricsGrid portfolio={basePortfolio()} loading={false} />);

    expect(screen.getByText('AI Spend Today')).toBeInTheDocument();
    expect(screen.getByText('$3.25')).toBeInTheDocument();
    expect(screen.getByText('$1.75 of $5.00 · enforced')).toBeInTheDocument();
  });

  it('says when the ceiling is not enforced rather than implying protection', () => {
    const portfolio = basePortfolio();
    (portfolio.ai_budget as { enforced: boolean }).enforced = false;

    render(<MetricsGrid portfolio={portfolio} loading={false} />);

    expect(screen.getByText('$1.75 of $5.00 · not enforced')).toBeInTheDocument();
  });

  it('raises a visible alert once the ceiling halts runs', () => {
    const portfolio = basePortfolio();
    Object.assign(portfolio.ai_budget as object, { exhausted: true, spent_usd: '5.00', remaining_usd: '0.00' });

    render(<MetricsGrid portfolio={portfolio} loading={false} />);

    expect(screen.getByRole('alert')).toHaveTextContent(/AI spend ceiling reached/i);
    expect(screen.getByRole('alert')).toHaveTextContent('AI_DAILY_SPEND_LIMIT_USD');
  });

  it('survives a portfolio with no metrics at all', () => {
    // The original regression: an absent payload blanked the cards instead of
    // rendering placeholders.
    const portfolio = basePortfolio({ metrics: undefined as never });

    expect(() => render(<MetricsGrid portfolio={portfolio} loading={false} />)).not.toThrow();
    expect(screen.getByText('Total Equity')).toBeInTheDocument();
  });

  it('survives a completely null portfolio', () => {
    expect(() => render(<MetricsGrid portfolio={null} loading />)).not.toThrow();
    expect(screen.getByText('AI Spend Today')).toBeInTheDocument();
  });

  it('never renders NaN, whatever the payload contains', () => {
    const portfolio = basePortfolio({
      metrics: {
        total_equity_usd: null,
        cash_balance_usd: 'garbage',
        positions_value_usd: undefined,
        unrealised_pnl_usd: Number.NaN,
        realised_pnl_today_usd: '',
        invested_cost_usd: null,
        unrealised_pnl_pct: null,
        realised_pnl_total_usd: null,
        daily_loss_limit_usd: null,
        daily_loss_used_usd: null,
        daily_loss_remaining_usd: null,
        unpriced_tickers: [],
        is_autonomy_blocked: false,
        block_reason: null,
      } as never,
      ai_budget: undefined as never,
    });

    const { container } = render(<MetricsGrid portfolio={portfolio} loading={false} />);

    expect(container.textContent).not.toMatch(/NaN|undefined|Infinity/);
  });

  it('warns when positions could not be priced', () => {
    const portfolio = basePortfolio();
    (portfolio.metrics as { unpriced_tickers: string[] }).unpriced_tickers = ['XYZ'];

    render(<MetricsGrid portfolio={portfolio} loading={false} />);

    expect(screen.getByText(/Unpriced: XYZ/)).toBeInTheDocument();
  });

  it('shows the block reason when autonomy is blocked', () => {
    const portfolio = basePortfolio();
    Object.assign(portfolio.metrics as object, {
      is_autonomy_blocked: true,
      block_reason: 'Daily loss limit reached.',
    });

    render(<MetricsGrid portfolio={portfolio} loading={false} />);

    expect(screen.getByRole('alert')).toHaveTextContent('Daily loss limit reached.');
  });
});
