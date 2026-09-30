import { useState, type ReactElement } from 'react';
import { cn } from '../lib/cn';
import { formatCurrency, formatQuantity, formatRelativeTime } from '../lib/format';
import { useNow } from '../hooks/useNow';
import type { Recommendation } from '../types';
import { Badge, SentimentBadge, StatusBadge } from './Badge';
import { EmptyState, ErrorState, SkeletonRows, Spinner } from './Feedback';
import { Panel } from './Panel';

export type DecisionKind = 'approve' | 'reject';

export interface RecommendationsPanelProps {
  pending: Recommendation[];
  history: Recommendation[];
  loading: boolean;
  error: string | null;
  busyIds: ReadonlySet<number>;
  onDecide: (id: number, decision: DecisionKind) => void;
  onRetry: () => void;
  className?: string;
}

function Reasoning({ text }: { text: string | null }): ReactElement | null {
  const [expanded, setExpanded] = useState(false);
  if (!text) return null;

  return (
    <>
      <p
        className={cn(
          'mt-1.5 whitespace-pre-wrap break-words text-[11px] leading-relaxed text-slate-400',
          !expanded && 'line-clamp-3',
        )}
      >
        {text}
      </p>
      {text.length > 180 || expanded ? (
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          aria-expanded={expanded}
          className="mt-1 rounded font-mono text-[10px] uppercase tracking-wider text-emerald-400/90 transition-colors hover:text-emerald-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400/60"
        >
          {expanded ? 'Hide reasoning' : 'Show reasoning'}
        </button>
      ) : null}
    </>
  );
}

function PendingCard({
  recommendation,
  busy,
  onDecide,
  now,
}: {
  recommendation: Recommendation;
  busy: boolean;
  onDecide: (id: number, decision: DecisionKind) => void;
  now: number;
}): ReactElement {
  return (
    <li className="animate-feed-in rounded-lg border border-amber-500/25 bg-amber-500/[0.04] p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm font-semibold tracking-wide text-slate-100">
          {recommendation.ticker}
        </span>
        <Badge tone={recommendation.action === 'BUY' ? 'emerald' : 'rose'}>{recommendation.action}</Badge>
        <SentimentBadge sentiment={recommendation.sentiment} />
        <StatusBadge status={recommendation.status} />
        <span className="ml-auto font-mono text-[10px] text-slate-500">
          {formatRelativeTime(recommendation.created_at, now)}
        </span>
      </div>

      <dl className="mt-2 grid grid-cols-3 gap-2 font-mono text-[11px] tabular-nums">
        <div>
          <dt className="text-[10px] uppercase tracking-wider text-slate-500">Qty</dt>
          <dd className="text-slate-200">{formatQuantity(recommendation.amount)}</dd>
        </div>
        <div>
          <dt className="text-[10px] uppercase tracking-wider text-slate-500">Price</dt>
          <dd className="text-slate-200">{formatCurrency(recommendation.price)}</dd>
        </div>
        <div>
          <dt className="text-[10px] uppercase tracking-wider text-slate-500">Notional</dt>
          <dd className="text-slate-200">{formatCurrency(recommendation.notional_usd)}</dd>
        </div>
      </dl>

      <Reasoning text={recommendation.reasoning} />

      {recommendation.expires_at ? (
        <p className="mt-1.5 font-mono text-[10px] uppercase tracking-wider text-amber-300/80">
          Expires {formatRelativeTime(recommendation.expires_at, now)}
        </p>
      ) : null}

      <div className="mt-2.5 flex items-center gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => onDecide(recommendation.id, 'approve')}
          className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-md bg-emerald-500 px-3 py-1.5 text-xs font-semibold text-slate-950 transition-colors hover:bg-emerald-400 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-300 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {busy ? <Spinner className="border-slate-900/40 border-t-slate-900" /> : null}
          Approve
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => onDecide(recommendation.id, 'reject')}
          className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-md border border-rose-500/40 px-3 py-1.5 text-xs font-semibold text-rose-300 transition-colors hover:bg-rose-500/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-rose-400/70 disabled:cursor-not-allowed disabled:opacity-60"
        >
          Reject
        </button>
      </div>
    </li>
  );
}

function HistoryRow({ recommendation, now }: { recommendation: Recommendation; now: number }): ReactElement {
  return (
    <li className="flex items-center gap-2 rounded px-1.5 py-1.5 opacity-70 transition-opacity hover:opacity-100">
      <span className="w-14 shrink-0 truncate font-mono text-[11px] font-semibold text-slate-400">
        {recommendation.ticker}
      </span>
      <Badge tone={recommendation.action === 'BUY' ? 'emerald' : 'rose'}>{recommendation.action}</Badge>
      <span className="font-mono text-[11px] tabular-nums text-slate-500">
        {formatCurrency(recommendation.notional_usd)}
      </span>
      <span className="ml-auto flex items-center gap-2">
        <StatusBadge status={recommendation.status} />
        <span className="hidden font-mono text-[10px] text-slate-600 sm:inline">
          {formatRelativeTime(recommendation.decided_at ?? recommendation.created_at, now)}
        </span>
      </span>
    </li>
  );
}

/** Pending recommendations with approve/reject, plus a muted decision history. */
export function RecommendationsPanel({
  pending,
  history,
  loading,
  error,
  busyIds,
  onDecide,
  onRetry,
  className,
}: RecommendationsPanelProps): ReactElement {
  const now = useNow(15_000);
  const [showAllHistory, setShowAllHistory] = useState(false);
  const visibleHistory = showAllHistory ? history : history.slice(0, 6);

  return (
    <Panel
      title="Recommendations"
      subtitle={
        pending.length > 0
          ? `${pending.length} awaiting your decision`
          : 'No pending decisions — the agent is working'
      }
      actions={
        pending.length > 0 ? <Badge tone="amber">{pending.length} pending</Badge> : null
      }
      bodyClassName="p-3"
      className={className}
    >
      {error ? (
        <ErrorState message={error} onRetry={onRetry} />
      ) : loading && pending.length === 0 && history.length === 0 ? (
        <SkeletonRows rows={3} />
      ) : (
        <div className="space-y-3">
          {pending.length === 0 ? (
            <EmptyState
              title="Nothing to approve"
              description={
                history.length > 0
                  ? 'All recommendations have been decided. New ones arrive in real time.'
                  : 'Trade proposals from the agent will appear here for approval.'
              }
              compact
            />
          ) : (
            <ul className="space-y-2.5">
              {pending.map((recommendation) => (
                <PendingCard
                  key={recommendation.id}
                  recommendation={recommendation}
                  busy={busyIds.has(recommendation.id)}
                  onDecide={onDecide}
                  now={now}
                />
              ))}
            </ul>
          )}

          {history.length > 0 ? (
            <div className="border-t border-slate-800/70 pt-2">
              <div className="flex items-center justify-between px-1.5 pb-1">
                <h3 className="font-mono text-[10px] uppercase tracking-[0.16em] text-slate-600">
                  History
                </h3>
                {history.length > 6 ? (
                  <button
                    type="button"
                    onClick={() => setShowAllHistory((value) => !value)}
                    className="rounded font-mono text-[10px] uppercase tracking-wider text-slate-500 transition-colors hover:text-slate-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-slate-500"
                  >
                    {showAllHistory ? 'Show less' : `Show all ${history.length}`}
                  </button>
                ) : null}
              </div>
              <ul className="max-h-52 space-y-0.5 overflow-y-auto">
                {visibleHistory.map((recommendation) => (
                  <HistoryRow key={recommendation.id} recommendation={recommendation} now={now} />
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      )}
    </Panel>
  );
}
