import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { PortfolioLimits } from './PortfolioLimits';
import type { Portfolio } from '../types';

/**
 * The two portfolio ceilings.
 *
 * The ceilings are the newest fields on the portfolio payload and the ones most
 * likely to be absent — they were added to the model after the serializer was
 * written. A missing ceiling must read as "not published", never as `0%`, which
 * would describe a portfolio forbidden from holding anything.
 */

const portfolio = (overrides: Partial<Portfolio> = {}): Portfolio =>
  ({
    id: 1,
    username: 'demo',
    balance_usd: '20000.00',
    risk_profile: 'high',
    risk_profile_display: 'Aggressive',
    is_autonomous: true,
    max_trade_allocation_pct: '5.00',
    max_trade_budget_usd: '1000.00',
    daily_loss_limit_usd: '500.00',
    max_portfolio_exposure_pct: '90.00',
    max_ticker_concentration_pct: '30.00',
    created_at: '2026-09-30T00:00:00Z',
    metrics: {
      total_equity_usd: '50000.00',
      cash_balance_usd: '30000.00',
      positions_value_usd: '20000.00',
      invested_cost_usd: '19000.00',
      unrealised_pnl_usd: '1000.00',
      unrealised_pnl_pct: '5.26',
      realised_pnl_today_usd: '0.00',
      realised_pnl_total_usd: '0.00',
      daily_loss_limit_usd: '500.00',
      daily_loss_used_usd: '0.00',
      daily_loss_remaining_usd: '500.00',
      unpriced_tickers: [],
      is_autonomy_blocked: false,
      block_reason: null,
    },
    assets: [
      {
        id: 1,
        ticker: 'BTC',
        amount: '0.12',
        avg_purchase_price: '80000.00',
        market_price: '83333.33',
        price_source: 'live',
        cost_basis_usd: '9600.00',
        market_value_usd: '10000.00',
        unrealised_pnl_usd: '400.00',
        unrealised_pnl_pct: '4.17',
        allocation_pct: '20.00',
      },
      {
        id: 2,
        ticker: 'AAPL',
        amount: '10',
        avg_purchase_price: '100.00',
        market_price: '100.00',
        price_source: 'live',
        cost_basis_usd: '1000.00',
        market_value_usd: '1000.00',
        unrealised_pnl_usd: '0.00',
        unrealised_pnl_pct: '0.00',
        allocation_pct: '2.00',
      },
    ],
    ...overrides,
  }) as unknown as Portfolio;

describe('PortfolioLimits', () => {
  it('shows invested capital against the exposure ceiling', () => {
    render(<PortfolioLimits portfolio={portfolio()} loading={false} />);

    expect(screen.getByText('Exposure ceiling')).toBeInTheDocument();
    expect(screen.getByText('40.0%')).toBeInTheDocument();
    expect(
      screen.getByText('$20,000.00 of $45,000.00 ceiling (90.00% of equity) · $25,000.00 headroom'),
    ).toBeInTheDocument();
  });

  it('shows the largest position against the concentration ceiling', () => {
    render(<PortfolioLimits portfolio={portfolio()} loading={false} />);

    expect(screen.getByText('Concentration ceiling')).toBeInTheDocument();
    expect(screen.getByText('20.0%')).toBeInTheDocument();
    expect(
      screen.getByText('BTC $10,000.00 of $15,000.00 ceiling (30.00% of equity) · $5,000.00 headroom'),
    ).toBeInTheDocument();
  });

  it('says when a position is over its ceiling rather than hiding it', () => {
    const over = portfolio();
    over.assets[0]!.market_value_usd = '30000.00';

    render(<PortfolioLimits portfolio={over} loading={false} />);

    expect(screen.getByText('60.0%')).toBeInTheDocument();
    expect(screen.getByText(/BTC \$30,000\.00 of \$15,000\.00 ceiling/)).toBeInTheDocument();
    expect(screen.getByText(/over by \$15,000\.00/)).toBeInTheDocument();
    expect(screen.getByText('60.0%').className).toContain('rose');
  });

  it('renders a missing ceiling as not published, not as a 0% ceiling', () => {
    // This is the shape the deployed API currently returns: both fields absent.
    const withoutCeilings = portfolio();
    delete withoutCeilings.max_portfolio_exposure_pct;
    delete withoutCeilings.max_ticker_concentration_pct;

    const { container } = render(<PortfolioLimits portfolio={withoutCeilings} loading={false} />);

    expect(screen.getAllByText(/ceiling not published/)).toHaveLength(2);
    expect(container.textContent).not.toMatch(/0\.00%/);
    // The readings themselves are still shown; only the comparison is unknown.
    expect(screen.getByText('40.0%')).toBeInTheDocument();
    expect(screen.getByText('20.0%')).toBeInTheDocument();
  });

  it('survives a hostile portfolio without rendering NaN', () => {
    const hostile = {
      metrics: { total_equity_usd: 'garbage', positions_value_usd: null },
      assets: [{ ticker: undefined, market_value_usd: undefined, allocation_pct: 'NaN' }],
      max_portfolio_exposure_pct: undefined,
      max_ticker_concentration_pct: Number.NaN,
    } as unknown as Portfolio;

    const { container } = render(<PortfolioLimits portfolio={hostile} loading={false} />);

    expect(container.textContent).not.toMatch(/NaN|undefined|Infinity/);
  });

  it('renders placeholders while the portfolio is still loading', () => {
    expect(() => render(<PortfolioLimits portfolio={null} loading />)).not.toThrow();
    expect(screen.queryByText('Exposure ceiling')).toBeNull();
  });
});
