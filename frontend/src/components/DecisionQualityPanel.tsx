import type { ReactElement } from 'react';
import { cn } from '../lib/cn';
import { formatCurrency, formatDateTime, formatPercent, toNumber, toneTextClass } from '../lib/format';
import type { AttributionResponse, AttributionScorecard, DecisionOutcomeRow } from '../types';
import { Badge, SideBadge } from './Badge';
import { EmptyState, ErrorState, SkeletonRows, Spinner } from './Feedback';
import { Panel } from './Panel';

/**
 * "Was the agent right?" — the question the audit trail never answered.
 *
 * Two rules shape every line of this panel:
 *
 * 1. **Not measured is not zero.** `hit_rate_pct` and `avg_alpha_pct` are `null`
 *    when nothing has been scored, or when no benchmark was available. They
 *    render as `—`. Showing `0%` would report a failure that was never observed.
 * 2. **A HOLD is not a miss.** `directional_hit` is `null` for a decision that
 *    made no directional claim, and it gets its own label rather than being
 *    folded in with the misses — the backend separates them precisely so the
 *    measured hit rate is not dragged down by caution.
 */

export interface DecisionQualityPanelProps {
  scorecard: AttributionResponse | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  className?: string;
}

const DASH = '—';

/** `1` → `"1d"`. Unparseable input becomes an em dash, never `"NaNd"`. */
export function formatHorizon(days: number | string | null | undefined): string {
  const parsed = toNumber(days);
  if (parsed === null) return DASH;
  return `${parsed}d`;
}

/**
 * `26.7` → `"26.7%"`; `null` → `"—"`.
 *
 * The distinction is the point of the panel: `null` means nothing has been
 * measured, `0` means every scored call was wrong, and they must not look alike.
 */
export function formatHitRate(value: number | string | null | undefined): string {
  const parsed = toNumber(value);
  if (parsed === null) return DASH;
  return formatPercent(parsed, { signed: false, decimals: 1 });
}

/** A hit rate below a coin flip is not neutral news, and is not coloured as such. */
export function hitRateTextClass(value: number | string | null | undefined): string {
  const parsed = toNumber(value);
  if (parsed === null) return 'text-slate-400';
  if (parsed >= 50) return 'text-emerald-400';
  if (parsed >= 40) return 'text-amber-300';
  return 'text-rose-400';
}

export type DecisionOutcome = 'hit' | 'miss' | 'not-a-call';

/** Strict on purpose: only an explicit `true` is a hit, only an explicit `false` a miss. */
export function outcomeOf(row: Pick<DecisionOutcomeRow, 'directional_hit'>): DecisionOutcome {
  if (row?.directional_hit === true) return 'hit';
  if (row?.directional_hit === false) return 'miss';
  return 'not-a-call';
}

const OUTCOME_BADGE: Record<DecisionOutcome, { label: string; tone: 'emerald' | 'rose' | 'slate' }> = {
  hit: { label: 'Hit', tone: 'emerald' },
  miss: { label: 'Miss', tone: 'rose' },
  'not-a-call': { label: 'No call', tone: 'slate' },
};

function OutcomeBadge({ outcome }: { outcome: DecisionOutcome }): ReactElement {
  const { label, tone } = OUTCOME_BADGE[outcome];
  return (
    <Badge
      tone={tone}
      title={
        outcome === 'not-a-call'
          ? 'Made no directional claim (e.g. a HOLD) — neither a hit nor a miss'
          : undefined
      }
    >
      {label}
    </Badge>
  );
}

function ScorecardTile({
  scorecard,
  benchmark,
}: {
  scorecard: AttributionScorecard;
  benchmark: string;
}): ReactElement {
  const horizon = formatHorizon(scorecard?.horizon_days);
  const evaluated = toNumber(scorecard?.evaluated) ?? 0;
  const directional = toNumber(scorecard?.directional_calls) ?? 0;
  const hits = toNumber(scorecard?.hits) ?? 0;
  const scored = evaluated > 0;

  const hitRate = toNumber(scorecard?.hit_rate_pct);
  // `avg_decision_return_pct` is `"0.0000"` when nothing has been evaluated, not
  // null. With no outcomes that zero is an artefact, so it is shown as not
  // measured rather than as a break-even result.
  const avgDecision = scored ? toNumber(scorecard?.avg_decision_return_pct) : null;
  const avgAlpha = scored ? toNumber(scorecard?.avg_alpha_pct) : null;
  const benchmarked = toNumber(scorecard?.benchmarked) ?? 0;
  const beat = toNumber(scorecard?.beat_benchmark) ?? 0;

  return (
    <section
      aria-label={`${horizon} horizon scorecard`}
      className="min-w-0 rounded-lg border border-slate-800/70 bg-slate-950/40 px-3 py-2.5"
    >
      <div className="flex items-center justify-between gap-2">
        <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
          {horizon} horizon
        </p>
        <p className="font-mono text-[10px] tabular-nums text-slate-600">{evaluated} scored</p>
      </div>

      <div className="mt-1.5 flex items-baseline gap-2">
        <p className={cn('font-mono text-xl font-semibold leading-6 tabular-nums', hitRateTextClass(hitRate))}>
          {formatHitRate(hitRate)}
        </p>
        <p className="font-mono text-[10px] uppercase tracking-wider text-slate-500">hit rate</p>
      </div>

      <p className="mt-0.5 text-[11px] text-slate-500">
        {hitRate === null
          ? scored
            ? 'no directional calls scored'
            : 'nothing scored yet'
          : `${hits} of ${directional} directional calls`}
      </p>

      <dl className="mt-2 space-y-0.5 border-t border-slate-800/60 pt-2 font-mono text-[11px] tabular-nums">
        <div className="flex items-center justify-between gap-2">
          <dt className="text-slate-600">avg decision</dt>
          <dd className={avgDecision === null ? 'text-slate-400' : toneTextClass(avgDecision)}>
            {avgDecision === null ? DASH : formatPercent(avgDecision)}
          </dd>
        </div>
        <div className="flex items-center justify-between gap-2">
          <dt className="truncate text-slate-600">avg alpha vs {benchmark}</dt>
          <dd className={avgAlpha === null ? 'text-slate-400' : toneTextClass(avgAlpha)}>
            {avgAlpha === null ? DASH : formatPercent(avgAlpha)}
          </dd>
        </div>
        <div className="flex items-center justify-between gap-2">
          <dt className="text-slate-600">beat benchmark</dt>
          <dd className="text-slate-300">{benchmarked > 0 ? `${beat} of ${benchmarked}` : DASH}</dd>
        </div>
      </dl>
    </section>
  );
}

function OutcomeRow({ row, benchmark }: { row: DecisionOutcomeRow; benchmark: string }): ReactElement {
  const ticker = (row?.ticker ?? '').trim().toUpperCase() || DASH;
  const action = (row?.action ?? '').trim().toUpperCase();
  const outcome = outcomeOf(row);
  const decisionReturn = toNumber(row?.decision_return_pct);
  const benchmarkReturn = toNumber(row?.benchmark_return_pct);

  const detail = [
    `decision #${row?.decision_id ?? DASH}`,
    action ? `${action} @ ${formatCurrency(row?.price_at_decision)}` : null,
    benchmarkReturn === null ? `no ${benchmark} return recorded` : `${benchmark} ${formatPercent(benchmarkReturn)}`,
    `scored ${formatDateTime(row?.evaluated_at)}`,
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <li
      title={detail}
      className="flex items-center gap-2 rounded-md border border-slate-800/60 bg-slate-950/40 px-2 py-1.5 transition-colors duration-300 hover:border-slate-700/80 hover:bg-slate-900/40"
    >
      <span className="w-14 shrink-0 truncate font-mono text-xs font-semibold text-slate-100">{ticker}</span>
      <SideBadge side={action || null} />
      <span className="shrink-0 font-mono text-[10px] uppercase tracking-wider text-slate-500">
        {formatHorizon(row?.horizon_days)}
      </span>
      <span
        className={cn(
          'ml-auto shrink-0 font-mono text-xs tabular-nums',
          decisionReturn === null ? 'text-slate-400' : toneTextClass(decisionReturn),
        )}
      >
        {decisionReturn === null ? DASH : formatPercent(decisionReturn)}
      </span>
      <span className="shrink-0">
        <OutcomeBadge outcome={outcome} />
      </span>
    </li>
  );
}

export function DecisionQualityPanel({
  scorecard,
  loading,
  error,
  onRetry,
  className,
}: DecisionQualityPanelProps): ReactElement {
  const horizons = scorecard?.horizons ?? [];
  const recent = scorecard?.recent ?? [];
  const benchmark = (scorecard?.benchmark ?? '').trim().toUpperCase() || 'benchmark';

  const subtitle = scorecard
    ? `vs ${benchmark} · ${recent.length} recent scored decision${recent.length === 1 ? '' : 's'}`
    : 'Awaiting scorecard';

  return (
    <Panel
      title="Decision Quality"
      subtitle={subtitle}
      actions={
        <button
          type="button"
          onClick={onRetry}
          disabled={loading}
          title="Refetch the decision scorecard"
          aria-label="Refresh decision quality"
          className="inline-flex items-center gap-1.5 rounded-md border border-slate-700 bg-slate-950/60 px-2 py-1 font-mono text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-300 transition-colors hover:bg-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400/60 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {loading ? <Spinner className="h-3 w-3" /> : null}
          Refresh
        </button>
      }
      bodyClassName="p-3 sm:p-4"
      className={className}
    >
      {error && scorecard === null ? (
        <ErrorState message={error} onRetry={onRetry} />
      ) : loading && scorecard === null ? (
        <SkeletonRows rows={4} />
      ) : scorecard === null ? (
        <EmptyState
          title="No scorecard yet"
          description="Outcomes are scored once each decision's horizon has elapsed."
        />
      ) : (
        <div className="space-y-3">
          {error ? <ErrorState compact message={`Could not refresh — ${error}`} onRetry={onRetry} /> : null}

          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            {horizons.map((horizon, index) => (
              <ScorecardTile key={horizon?.horizon_days ?? index} scorecard={horizon} benchmark={benchmark} />
            ))}
          </div>

          <section aria-label="Recent scored decisions" className="min-w-0">
            <h3 className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500">
              Recent scored decisions
            </h3>

            {recent.length === 0 ? (
              <EmptyState
                compact
                className="mt-2"
                title="Nothing scored yet"
                description="The individual decisions behind the scorecard appear here once their horizon elapses."
              />
            ) : (
              <ul className="mt-2 max-h-80 space-y-1.5 overflow-y-auto pr-1">
                {recent.map((row, index) => (
                  <OutcomeRow key={row?.id ?? `row-${index}`} row={row} benchmark={benchmark} />
                ))}
              </ul>
            )}
          </section>
        </div>
      )}
    </Panel>
  );
}
