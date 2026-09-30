import { useMemo, type ReactElement, type ReactNode } from 'react';
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipProps,
} from 'recharts';
import { cn } from '../lib/cn';
import {
  formatAxisTime,
  formatCurrency,
  formatDateTime,
  formatPercent,
  toNumber,
  toNumberOrZero,
  toneTextClass,
} from '../lib/format';
import { SNAPSHOT_RANGES } from '../hooks/useSnapshots';
import type { PortfolioSnapshot, SnapshotRange } from '../types';
import { EmptyState, ErrorState, Skeleton } from './Feedback';
import { Panel } from './Panel';

interface EquityPoint {
  ts: number;
  equity: number;
  cash: number;
  delta: number | null;
}

export interface EquityChartProps {
  snapshots: PortfolioSnapshot[];
  range: SnapshotRange;
  onRangeChange: (range: SnapshotRange) => void;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  className?: string;
}

function RangeSelector({
  value,
  onChange,
}: {
  value: SnapshotRange;
  onChange: (range: SnapshotRange) => void;
}): ReactElement {
  return (
    <div
      role="tablist"
      aria-label="Equity chart range"
      className="inline-flex items-center gap-0.5 rounded-md border border-slate-800 bg-slate-950/60 p-0.5"
    >
      {SNAPSHOT_RANGES.map((range) => {
        const active = range === value;
        return (
          <button
            key={range}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(range)}
            className={cn(
              'rounded px-2 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider transition-colors duration-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400/60',
              active
                ? 'bg-emerald-500/15 text-emerald-300'
                : 'text-slate-500 hover:bg-slate-800/70 hover:text-slate-300',
            )}
          >
            {range}
          </button>
        );
      })}
    </div>
  );
}

function renderTooltip(props: TooltipProps<number, string>): ReactNode {
  const { active, payload } = props;
  if (!active || !payload || payload.length === 0) return null;

  const first = payload[0];
  if (!first) return null;
  const point = first.payload as EquityPoint | undefined;
  if (!point) return null;

  return (
    <div className="rounded-lg border border-slate-700 bg-slate-950/95 px-3 py-2 shadow-panel">
      <p className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
        {formatDateTime(new Date(point.ts).toISOString())}
      </p>
      <p className="mt-1 font-mono text-sm font-semibold tabular-nums text-slate-100">
        {formatCurrency(point.equity)}
      </p>
      <dl className="mt-1 space-y-0.5 font-mono text-[10px] tabular-nums text-slate-400">
        <div className="flex items-center justify-between gap-4">
          <dt>Cash</dt>
          <dd>{formatCurrency(point.cash)}</dd>
        </div>
        <div className="flex items-center justify-between gap-4">
          <dt>Δ prev</dt>
          <dd className={toneTextClass(point.delta)}>
            {point.delta === null ? '—' : formatCurrency(point.delta, { signed: true })}
          </dd>
        </div>
      </dl>
    </div>
  );
}

function formatAxisCurrency(value: number): string {
  return Math.abs(value) >= 1_000
    ? formatCurrency(value, { compact: true, decimals: 1 })
    : formatCurrency(value, { decimals: 0 });
}

/** `AreaChart` of total equity over the selected window. */
export function EquityChart({
  snapshots,
  range,
  onRangeChange,
  loading,
  error,
  onRetry,
  className,
}: EquityChartProps): ReactElement {
  const points = useMemo<EquityPoint[]>(() => {
    const built: EquityPoint[] = [];
    let previous: number | null = null;

    for (const snapshot of snapshots) {
      const ts = Date.parse(snapshot.captured_at);
      const equity = toNumber(snapshot.total_equity_usd);
      if (Number.isNaN(ts) || equity === null) continue;

      built.push({
        ts,
        equity,
        cash: toNumberOrZero(snapshot.cash_balance_usd),
        delta: previous === null ? null : equity - previous,
      });
      previous = equity;
    }

    return built.sort((a, b) => a.ts - b.ts);
  }, [snapshots]);

  const domain = useMemo<[number | string, number | string]>(() => {
    if (points.length === 0) return ['auto', 'auto'];
    let min = Number.POSITIVE_INFINITY;
    let max = Number.NEGATIVE_INFINITY;
    for (const point of points) {
      if (point.equity < min) min = point.equity;
      if (point.equity > max) max = point.equity;
    }
    const pad = Math.max((max - min) * 0.15, Math.abs(max) * 0.001, 1);
    return [min - pad, max + pad];
  }, [points]);

  const first = points[0];
  const last = points[points.length - 1];
  const windowDelta = first && last ? last.equity - first.equity : null;
  const windowPct =
    first && last && first.equity !== 0 ? ((last.equity - first.equity) / first.equity) * 100 : null;

  const showDate = range !== '24H';

  return (
    <Panel
      title="Equity Curve"
      subtitle={
        points.length > 0
          ? `${points.length} snapshot${points.length === 1 ? '' : 's'} · ${
              range === 'ALL' ? 'all time' : `last ${range}`
            }`
          : 'Total account value over time'
      }
      actions={
        <>
          {windowDelta !== null ? (
            <span className={cn('font-mono text-xs tabular-nums', toneTextClass(windowDelta))}>
              {formatCurrency(windowDelta, { signed: true })}
              {windowPct !== null ? ` (${formatPercent(windowPct)})` : ''}
            </span>
          ) : null}
          <RangeSelector value={range} onChange={onRangeChange} />
        </>
      }
      bodyClassName="p-2 pt-3 sm:p-4"
      className={className}
    >
      {error ? (
        <ErrorState message={error} onRetry={onRetry} />
      ) : loading && points.length === 0 ? (
        <Skeleton className="h-[280px] w-full" />
      ) : points.length === 0 ? (
        <EmptyState
          title="Collecting data…"
          description="The equity curve appears once the agent records its first portfolio snapshot. Snapshots are captured on every agent cycle."
          className="h-[280px]"
        />
      ) : (
        <div className="h-[280px] w-full">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
              <defs>
                <linearGradient id="equityGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#34d399" stopOpacity={0.45} />
                  <stop offset="55%" stopColor="#34d399" stopOpacity={0.12} />
                  <stop offset="100%" stopColor="#34d399" stopOpacity={0} />
                </linearGradient>
              </defs>

              <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" vertical={false} />

              <XAxis
                dataKey="ts"
                type="number"
                scale="time"
                domain={['dataMin', 'dataMax']}
                tickFormatter={(value: number) => formatAxisTime(value, showDate)}
                tick={{ fill: '#64748b', fontSize: 10, fontFamily: 'ui-monospace, monospace' }}
                axisLine={{ stroke: '#1e293b' }}
                tickLine={false}
                minTickGap={44}
              />

              <YAxis
                domain={domain}
                tickFormatter={formatAxisCurrency}
                tick={{ fill: '#64748b', fontSize: 10, fontFamily: 'ui-monospace, monospace' }}
                axisLine={false}
                tickLine={false}
                width={62}
              />

              <Tooltip
                content={renderTooltip}
                cursor={{ stroke: '#334155', strokeDasharray: '3 3' }}
              />

              <Area
                type="monotone"
                dataKey="equity"
                name="Total equity"
                stroke="#34d399"
                strokeWidth={2}
                fill="url(#equityGradient)"
                activeDot={{ r: 3.5, fill: '#34d399', stroke: '#052e1b', strokeWidth: 2 }}
                animationDuration={450}
              />

              <Line
                type="monotone"
                dataKey="cash"
                name="Cash"
                stroke="#475569"
                strokeWidth={1.25}
                strokeDasharray="4 4"
                dot={false}
                activeDot={false}
                animationDuration={450}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </Panel>
  );
}
