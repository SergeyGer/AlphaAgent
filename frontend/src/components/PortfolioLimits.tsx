import type { ReactElement } from 'react';
import { formatCurrency, formatPercent, toNumber } from '../lib/format';
import type { Portfolio } from '../types';
import { Skeleton } from './Feedback';
import { ProgressBar, type MetricProgress } from './MetricCard';

/**
 * The two portfolio-level risk ceilings, as compact meters.
 *
 * These are deliberately *not* two more headline cards: the seven metrics above
 * answer "what do I hold?", these answer "how close is that to the limits?" and
 * belong underneath, at secondary weight.
 *
 * Both ceilings are optional on the wire. A missing ceiling is rendered as
 * "not published" rather than as `0%` — a 0% ceiling would read as a ban on
 * holding anything, which is the opposite of "unknown".
 */

export interface PortfolioLimitsProps {
  portfolio: Portfolio | null;
  loading: boolean;
}

const DASH = '—';

/** Usage ratio at which the meter turns amber, matching the metric cards. */
const CAUTION_RATIO = 0.9;

function usageClass(ratio: number | null): string {
  if (ratio !== null && ratio > 1) return 'text-rose-400';
  if (ratio !== null && ratio >= CAUTION_RATIO) return 'text-amber-300';
  return 'text-slate-100';
}

function Meter({
  label,
  value,
  valueClassName,
  progress,
  progressLabel,
  hint,
}: {
  label: string;
  value: string;
  valueClassName: string;
  progress: MetricProgress | null;
  progressLabel: string;
  hint: string;
}): ReactElement {
  return (
    <div className="min-w-0 space-y-1">
      <div className="flex flex-wrap items-baseline justify-between gap-x-2">
        <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-slate-500">{label}</p>
        <p className={`font-mono text-xs font-semibold tabular-nums ${valueClassName}`}>{value}</p>
      </div>

      {progress ? (
        <ProgressBar label={progressLabel} progress={progress} />
      ) : (
        <div className="h-1.5 w-full rounded-full bg-slate-800/60" aria-hidden="true" />
      )}

      <p className="truncate text-[11px] text-slate-500" title={hint}>
        {hint}
      </p>
    </div>
  );
}

export function PortfolioLimits({ portfolio, loading }: PortfolioLimitsProps): ReactElement {
  if (loading && portfolio === null) {
    return (
      <div className="grid grid-cols-1 gap-3 rounded-lg border border-slate-800/70 bg-slate-900/30 px-3 py-2.5 sm:grid-cols-2">
        <Skeleton className="h-9 w-full" />
        <Skeleton className="h-9 w-full" />
      </div>
    );
  }

  const metrics = portfolio?.metrics ?? null;
  const equity = toNumber(metrics?.total_equity_usd);
  const invested = toNumber(metrics?.positions_value_usd);

  const exposureCeilingPct = toNumber(portfolio?.max_portfolio_exposure_pct);
  const concentrationCeilingPct = toNumber(portfolio?.max_ticker_concentration_pct);

  // --- Exposure: invested capital vs the ceiling on total invested capital ---
  const exposureUsedPct =
    equity !== null && equity > 0 && invested !== null ? (invested / equity) * 100 : null;
  const exposureCeilingUsd =
    equity !== null && exposureCeilingPct !== null ? (equity * exposureCeilingPct) / 100 : null;
  const exposureRatio =
    exposureCeilingUsd !== null && exposureCeilingUsd > 0 && invested !== null
      ? invested / exposureCeilingUsd
      : null;

  const exposureValue = exposureUsedPct === null ? DASH : formatPercent(exposureUsedPct, { signed: false, decimals: 1 });
  const exposureProgress: MetricProgress | null =
    exposureCeilingUsd !== null && exposureCeilingUsd > 0 && invested !== null
      ? { value: invested, max: exposureCeilingUsd }
      : null;
  const exposureHint =
    exposureCeilingPct === null
      ? `${formatCurrency(invested)} invested of ${formatCurrency(equity)} equity · ceiling not published`
      : invested === null || exposureCeilingUsd === null
        ? 'Positions value unavailable'
        : `${formatCurrency(invested)} of ${formatCurrency(exposureCeilingUsd)} ceiling (${
            formatPercent(exposureCeilingPct, { signed: false })
          } of equity) · ${
            invested > exposureCeilingUsd
              ? `over by ${formatCurrency(invested - exposureCeilingUsd)}`
              : `${formatCurrency(exposureCeilingUsd - invested)} headroom`
          }`;

  // --- Concentration: the largest single position vs its ceiling ---
  const assets = portfolio?.assets ?? [];
  let largestTicker: string | null = null;
  let largestValue: number | null = null;
  let largestAllocationPct: number | null = null;

  for (const asset of assets) {
    const value = toNumber(asset?.market_value_usd);
    if (value === null) continue;
    if (largestValue === null || value > largestValue) {
      largestValue = value;
      largestTicker = (asset?.ticker ?? '').trim() || null;
      largestAllocationPct = toNumber(asset?.allocation_pct);
    }
  }

  const concentrationUsedPct =
    equity !== null && equity > 0 && largestValue !== null
      ? (largestValue / equity) * 100
      : largestAllocationPct;
  const concentrationCeilingUsd =
    equity !== null && concentrationCeilingPct !== null
      ? (equity * concentrationCeilingPct) / 100
      : null;
  const concentrationRatio =
    concentrationCeilingUsd !== null && concentrationCeilingUsd > 0 && largestValue !== null
      ? largestValue / concentrationCeilingUsd
      : null;

  const concentrationValue =
    concentrationUsedPct === null ? DASH : formatPercent(concentrationUsedPct, { signed: false, decimals: 1 });
  const concentrationProgress: MetricProgress | null =
    concentrationCeilingUsd !== null && concentrationCeilingUsd > 0 && largestValue !== null
      ? { value: largestValue, max: concentrationCeilingUsd }
      : null;
  const concentrationHint =
    largestValue === null
      ? 'No open positions'
      : concentrationCeilingPct === null
        ? `${largestTicker ?? DASH} ${formatCurrency(largestValue)} of equity · ceiling not published`
        : concentrationCeilingUsd === null
          ? `${largestTicker ?? DASH} ${formatCurrency(largestValue)} · equity unavailable`
          : `${largestTicker ?? DASH} ${formatCurrency(largestValue)} of ${formatCurrency(
              concentrationCeilingUsd,
            )} ceiling (${formatPercent(concentrationCeilingPct, { signed: false })} of equity) · ${
              largestValue > concentrationCeilingUsd
                ? `over by ${formatCurrency(largestValue - concentrationCeilingUsd)}`
                : `${formatCurrency(concentrationCeilingUsd - largestValue)} headroom`
            }`;

  return (
    <div className="grid grid-cols-1 gap-x-5 gap-y-2.5 rounded-lg border border-slate-800/70 bg-slate-900/30 px-3 py-2.5 sm:grid-cols-2">
      <Meter
        label="Exposure ceiling"
        value={exposureValue}
        valueClassName={usageClass(exposureRatio)}
        progress={exposureProgress}
        progressLabel="Invested capital vs exposure ceiling"
        hint={exposureHint}
      />
      <Meter
        label="Concentration ceiling"
        value={concentrationValue}
        valueClassName={usageClass(concentrationRatio)}
        progress={concentrationProgress}
        progressLabel="Largest position vs concentration ceiling"
        hint={concentrationHint}
      />
    </div>
  );
}
