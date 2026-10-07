import type { ReactElement } from 'react';
import { useValueFlash } from '../hooks/useValueFlash';
import {
  formatCurrency,
  formatPercent,
  formatRatioPercent,
  toNumber,
  toNumberOrZero,
  toneTextClass,
} from '../lib/format';
import type { Portfolio } from '../types';
import { MetricCard, type MetricCardProps } from './MetricCard';

interface LiveMetricProps extends Omit<MetricCardProps, 'flash' | 'loading'> {
  numeric: number | null;
  loading: boolean;
}

/** Metric card wired to the live-flash animation. */
function LiveMetric({ numeric, loading, ...cardProps }: LiveMetricProps): ReactElement {
  const flash = useValueFlash(numeric);
  return <MetricCard {...cardProps} flash={flash} loading={loading} />;
}

export interface MetricsGridProps {
  portfolio: Portfolio | null;
  loading: boolean;
}

/**
 * Six headline metrics. All money values arrive as decimal strings and are
 * parsed before formatting, so the UI can never render `NaN`.
 */
export function MetricsGrid({ portfolio, loading }: MetricsGridProps): ReactElement {
  const metrics = portfolio?.metrics ?? null;
  const assets = portfolio?.assets ?? [];
  const assetCount = assets.length;

  const equity = toNumber(metrics?.total_equity_usd);
  const cash = toNumber(metrics?.cash_balance_usd);
  const positions = toNumber(metrics?.positions_value_usd);
  const unrealised = toNumber(metrics?.unrealised_pnl_usd);
  const realisedToday = toNumber(metrics?.realised_pnl_today_usd);
  const lossRemaining = toNumber(metrics?.daily_loss_remaining_usd);
  const lossUsed = toNumberOrZero(metrics?.daily_loss_used_usd);
  const lossLimit = toNumberOrZero(metrics?.daily_loss_limit_usd);

  // AI spend is an operating limit, not a trading one, but it halts the agent
  // just as absolutely as the loss limit halts trading - so it is shown with the
  // same weight rather than buried in the meta strip.
  const aiBudget = portfolio?.ai_budget ?? null;
  const aiSpent = toNumberOrZero(aiBudget?.spent_usd);
  const aiLimit = toNumberOrZero(aiBudget?.limit_usd);
  const aiRemaining = toNumber(aiBudget?.remaining_usd);
  const aiEnforced = aiBudget?.enforced ?? true;
  const aiExhausted = aiBudget?.exhausted ?? false;
  const aiUsedRatio = aiLimit > 0 ? aiSpent / aiLimit : 0;
  const unpriced = metrics?.unpriced_tickers ?? [];

  const equityShare =
    equity !== null && equity > 0 && cash !== null ? formatRatioPercent(cash / equity) : null;

  const positionHintParts = [`${assetCount} position${assetCount === 1 ? '' : 's'}`];
  if (unpriced.length > 0) positionHintParts.push(`${unpriced.length} unpriced`);

  return (
    <div className="space-y-3">
      {metrics?.is_autonomy_blocked ? (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200"
        >
          <span aria-hidden="true" className="font-mono">
            ⚠
          </span>
          <p>
            <strong className="font-semibold">Autonomous trading is blocked.</strong>{' '}
            {metrics.block_reason ?? 'Risk limits prevent new positions right now.'}
          </p>
        </div>
      ) : null}

      {unpriced.length > 0 ? (
        <p className="font-mono text-[10px] uppercase tracking-wider text-amber-300/80">
          Unpriced: {unpriced.join(', ')} — marked at cost
        </p>
      ) : null}

      {aiExhausted ? (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200"
        >
          <span aria-hidden="true" className="font-mono">
            ⛔
          </span>
          <p>
            <strong className="font-semibold">AI spend ceiling reached.</strong> No further agent
            runs will be dispatched until 00:00 UTC. Raise{' '}
            <code className="font-mono">AI_DAILY_SPEND_LIMIT_USD</code> to continue.
          </p>
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-7">
        <LiveMetric
          label="Total Equity"
          numeric={equity}
          loading={loading}
          value={formatCurrency(metrics?.total_equity_usd)}
          hint={`Cost basis ${formatCurrency(metrics?.invested_cost_usd)}`}
        />

        <LiveMetric
          label="Cash Balance"
          numeric={cash}
          loading={loading}
          value={formatCurrency(metrics?.cash_balance_usd)}
          hint={equityShare ? `${equityShare} of equity` : 'Available buying power'}
        />

        <LiveMetric
          label="Positions Value"
          numeric={positions}
          loading={loading}
          value={formatCurrency(metrics?.positions_value_usd)}
          hint={positionHintParts.join(' · ')}
        />

        <LiveMetric
          label="Unrealised P&L"
          numeric={unrealised}
          loading={loading}
          value={formatCurrency(metrics?.unrealised_pnl_usd, { signed: true })}
          valueClassName={toneTextClass(metrics?.unrealised_pnl_usd)}
          hint={`${formatPercent(metrics?.unrealised_pnl_pct)} vs cost basis`}
        />

        <LiveMetric
          label="Realised P&L Today"
          numeric={realisedToday}
          loading={loading}
          value={formatCurrency(metrics?.realised_pnl_today_usd, { signed: true })}
          valueClassName={toneTextClass(metrics?.realised_pnl_today_usd)}
          hint={`All time ${formatCurrency(metrics?.realised_pnl_total_usd)}`}
        />

        <LiveMetric
          label="Daily Loss Budget"
          numeric={lossRemaining}
          loading={loading}
          value={formatCurrency(metrics?.daily_loss_remaining_usd)}
          valueClassName={
            lossLimit > 0 && lossUsed / lossLimit >= 0.9
              ? 'text-rose-400'
              : lossLimit > 0 && lossUsed / lossLimit >= 0.6
                ? 'text-amber-300'
                : 'text-slate-100'
          }
          hint={`${formatCurrency(metrics?.daily_loss_used_usd)} of ${formatCurrency(
            metrics?.daily_loss_limit_usd,
          )} consumed`}
          progress={{ value: lossUsed, max: lossLimit }}
        />

        <LiveMetric
          label="AI Spend Today"
          numeric={aiRemaining}
          loading={loading}
          value={formatCurrency(aiBudget?.remaining_usd)}
          valueClassName={
            aiExhausted || aiUsedRatio >= 0.9
              ? 'text-rose-400'
              : aiUsedRatio >= 0.6
                ? 'text-amber-300'
                : 'text-slate-100'
          }
          hint={
            aiEnforced
              ? `${formatCurrency(aiBudget?.spent_usd)} of ${formatCurrency(aiBudget?.limit_usd)} · enforced`
              : `${formatCurrency(aiBudget?.spent_usd)} of ${formatCurrency(aiBudget?.limit_usd)} · not enforced`
          }
          progress={{ value: aiSpent, max: aiLimit }}
        />
      </div>

      <dl className="flex flex-wrap items-center gap-x-5 gap-y-1.5 rounded-lg border border-slate-800/70 bg-slate-900/30 px-3 py-2 font-mono text-[10px] uppercase tracking-wider text-slate-500">
        <MetaItem label="Risk profile" value={portfolio?.risk_profile_display ?? portfolio?.risk_profile ?? '—'} />
        <MetaItem label="Max trade alloc" value={formatPercent(portfolio?.max_trade_allocation_pct, { signed: false })} />
        <MetaItem label="Max trade budget" value={formatCurrency(portfolio?.max_trade_budget_usd)} />
        <MetaItem label="Open positions" value={String(assetCount)} />
        <MetaItem
          label="Autonomy"
          value={portfolio ? (portfolio.is_autonomous ? 'engaged' : 'manual') : '—'}
          valueClassName={portfolio?.is_autonomous ? 'text-emerald-300' : 'text-slate-400'}
        />
      </dl>
    </div>
  );
}

function MetaItem({
  label,
  value,
  valueClassName,
}: {
  label: string;
  value: string;
  valueClassName?: string;
}): ReactElement {
  return (
    <div className="flex items-center gap-1.5">
      <dt className="text-slate-600">{label}</dt>
      <dd className={valueClassName ?? 'text-slate-300'}>{value}</dd>
    </div>
  );
}
