import type { ReactElement, ReactNode } from 'react';
import { cn } from '../lib/cn';
import type { FlashDirection } from '../hooks/useValueFlash';
import { clamp } from '../lib/format';
import { Skeleton } from './Feedback';

export interface MetricProgress {
  value: number;
  max: number;
  /** Colour intent for the bar; defaults to emerald → amber → rose by usage. */
  tone?: 'emerald' | 'amber' | 'rose' | 'sky';
}

export interface MetricCardProps {
  label: string;
  /** Pre-formatted value (never raw numbers — avoids `NaN` on screen). */
  value: string;
  valueClassName?: string;
  hint?: ReactNode;
  flash?: FlashDirection | null;
  progress?: MetricProgress;
  loading?: boolean;
  footer?: ReactNode;
}

const BAR_TONE: Record<NonNullable<MetricProgress['tone']>, string> = {
  emerald: 'bg-emerald-400',
  amber: 'bg-amber-400',
  rose: 'bg-rose-400',
  sky: 'bg-sky-400',
};

function flashAnimation(flash: FlashDirection | null | undefined): string {
  if (flash === 'up') return 'animate-flash-up';
  if (flash === 'down') return 'animate-flash-down';
  return '';
}

export function MetricCard({
  label,
  value,
  valueClassName,
  hint,
  flash,
  progress,
  loading = false,
  footer,
}: MetricCardProps): ReactElement {
  const usedPct = progress ? clamp((progress.value / (progress.max || 1)) * 100, 0, 100) : 0;
  const tone = progress?.tone ?? (usedPct >= 90 ? 'rose' : usedPct >= 60 ? 'amber' : 'emerald');

  return (
    <div className="flex min-w-0 flex-col gap-1.5 rounded-xl border border-slate-800/80 bg-slate-900/50 px-3.5 py-3 shadow-panel transition-colors duration-300 hover:border-slate-700">
      <p className="truncate font-mono text-[10px] uppercase tracking-[0.16em] text-slate-500">{label}</p>

      {loading ? (
        <Skeleton className="h-7 w-28" />
      ) : (
        <p
          className={cn(
            'truncate rounded font-mono text-[1.35rem] font-semibold leading-8 tabular-nums text-slate-100',
            flashAnimation(flash),
            valueClassName,
          )}
          title={value}
        >
          {value}
        </p>
      )}

      {progress && !loading ? (
        <div className="space-y-1">
          <div
            className="h-1.5 w-full overflow-hidden rounded-full bg-slate-800"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(usedPct)}
            aria-label={`${label} consumption`}
          >
            <div
              className={cn('h-full rounded-full transition-all duration-700 ease-terminal', BAR_TONE[tone])}
              style={{ width: `${usedPct}%` }}
            />
          </div>
        </div>
      ) : null}

      {loading ? (
        <Skeleton className="h-3 w-20" />
      ) : hint ? (
        <p className="truncate text-[11px] text-slate-500" title={typeof hint === 'string' ? hint : undefined}>
          {hint}
        </p>
      ) : null}

      {footer}
    </div>
  );
}
