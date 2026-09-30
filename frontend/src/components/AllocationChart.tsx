import { useMemo, type ReactElement, type ReactNode } from 'react';
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip, type TooltipProps } from 'recharts';
import { CASH_COLOR, paletteColor } from '../lib/colors';
import { formatCurrency, formatPercent, toNumber, toNumberOrZero } from '../lib/format';
import type { Portfolio } from '../types';
import { EmptyState, ErrorState, Skeleton } from './Feedback';
import { Panel } from './Panel';

interface AllocationSlice {
  name: string;
  value: number;
  pct: number;
  color: string;
  isCash: boolean;
}

export interface AllocationChartProps {
  portfolio: Portfolio | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  className?: string;
}

function renderTooltip(props: TooltipProps<number, string>): ReactNode {
  const { active, payload } = props;
  if (!active || !payload || payload.length === 0) return null;

  const first = payload[0];
  if (!first) return null;
  const slice = first.payload as AllocationSlice | undefined;
  if (!slice) return null;

  return (
    <div className="rounded-lg border border-slate-700 bg-slate-950/95 px-3 py-2 shadow-panel">
      <p className="flex items-center gap-2 font-mono text-[11px] font-semibold text-slate-100">
        <span className="h-2 w-2 rounded-sm" style={{ backgroundColor: slice.color }} />
        {slice.name}
      </p>
      <p className="mt-1 font-mono text-xs tabular-nums text-slate-300">{formatCurrency(slice.value)}</p>
      <p className="font-mono text-[10px] tabular-nums text-slate-500">
        {formatPercent(slice.pct, { signed: false })} of equity
      </p>
    </div>
  );
}

/** Donut of asset market value plus a synthetic cash slice. */
export function AllocationChart({
  portfolio,
  loading,
  error,
  onRetry,
  className,
}: AllocationChartProps): ReactElement {
  const slices = useMemo<AllocationSlice[]>(() => {
    if (!portfolio) return [];

    const rows: AllocationSlice[] = [];

    (portfolio.assets ?? []).forEach((asset, index) => {
      const value = toNumber(asset.market_value_usd);
      if (value === null || value <= 0) return;
      rows.push({ name: asset.ticker, value, pct: 0, color: paletteColor(index), isCash: false });
    });

    const cash = toNumberOrZero(portfolio.metrics?.cash_balance_usd);
    if (cash > 0) {
      rows.push({ name: 'Cash', value: cash, pct: 0, color: CASH_COLOR, isCash: true });
    }

    const total = rows.reduce((sum, row) => sum + row.value, 0);
    if (total <= 0) return [];

    return rows
      .map((row) => ({ ...row, pct: (row.value / total) * 100 }))
      .sort((a, b) => b.value - a.value);
  }, [portfolio]);

  const total = slices.reduce((sum, slice) => sum + slice.value, 0);
  const positionsValue = slices.filter((slice) => !slice.isCash).reduce((sum, slice) => sum + slice.value, 0);

  return (
    <Panel
      title="Allocation"
      subtitle={slices.length > 0 ? `${slices.length} slices · by market value` : 'Portfolio composition'}
      bodyClassName="p-3 sm:p-4"
      className={className}
    >
      {error ? (
        <ErrorState message={error} onRetry={onRetry} />
      ) : loading && slices.length === 0 ? (
        <div className="flex flex-col gap-3">
          <Skeleton className="mx-auto h-[180px] w-[180px] rounded-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      ) : slices.length === 0 ? (
        <EmptyState
          title="No positions yet"
          description="Once the agent opens its first position the allocation breakdown appears here."
        />
      ) : (
        <div className="flex flex-col gap-3">
          <div className="relative mx-auto h-[190px] w-full max-w-[240px]">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={slices}
                  dataKey="value"
                  nameKey="name"
                  innerRadius="62%"
                  outerRadius="90%"
                  paddingAngle={2}
                  stroke="#0b1120"
                  strokeWidth={2}
                  animationDuration={450}
                >
                  {slices.map((slice) => (
                    <Cell key={slice.name} fill={slice.color} />
                  ))}
                </Pie>
                <Tooltip content={renderTooltip} />
              </PieChart>
            </ResponsiveContainer>

            <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
              <span className="font-mono text-[9px] uppercase tracking-[0.18em] text-slate-500">Equity</span>
              <span className="font-mono text-sm font-semibold tabular-nums text-slate-100">
                {formatCurrency(total, { compact: true })}
              </span>
              <span className="font-mono text-[9px] tabular-nums text-slate-500">
                {formatCurrency(positionsValue, { compact: true })} invested
              </span>
            </div>
          </div>

          <ul className="-mx-1 max-h-60 space-y-0.5 overflow-y-auto px-1">
            {slices.map((slice) => (
              <li
                key={slice.name}
                className="flex items-center gap-2 rounded px-1.5 py-1 transition-colors hover:bg-slate-800/50"
              >
                <span
                  aria-hidden="true"
                  className="h-2.5 w-2.5 shrink-0 rounded-sm"
                  style={{ backgroundColor: slice.color }}
                />
                <span className="min-w-0 flex-1 truncate font-mono text-xs font-semibold text-slate-200">
                  {slice.name}
                </span>
                <span className="font-mono text-xs tabular-nums text-slate-300">
                  {formatCurrency(slice.value)}
                </span>
                <span className="w-14 shrink-0 text-right font-mono text-xs tabular-nums text-slate-500">
                  {formatPercent(slice.pct, { signed: false })}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Panel>
  );
}
