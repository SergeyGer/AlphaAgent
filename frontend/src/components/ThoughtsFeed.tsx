import { memo, useState, type ReactElement, type ReactNode } from 'react';
import { cn } from '../lib/cn';
import { deriveAction, normalizeDebateText, stageLabel, type ActionDescriptor } from '../lib/feed';
import { formatClockTime, formatCurrency, formatQuantity, formatRelativeTime } from '../lib/format';
import type { FeedItem } from '../types';
import { useNow } from '../hooks/useNow';
import { Badge, SentimentBadge } from './Badge';
import { EmptyState, ErrorState, SkeletonRows } from './Feedback';
import { Panel } from './Panel';

export interface ThoughtsFeedProps {
  items: FeedItem[];
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  /** Total rows retained in memory (for the subtitle). */
  cap: number;
  className?: string;
}

interface FeedRowProps {
  item: FeedItem;
  now: number;
}

type DebateTone = 'bull' | 'bear' | 'verdict';

const DEBATE_SECTION_CLASS: Record<DebateTone, string> = {
  bull: 'border-emerald-500/30 bg-emerald-500/[0.05]',
  bear: 'border-rose-500/30 bg-rose-500/[0.05]',
  verdict: 'border-slate-700/70 bg-slate-950/50',
};

const DEBATE_HEADING_CLASS: Record<DebateTone, string> = {
  bull: 'text-emerald-300',
  bear: 'text-rose-300',
  verdict: 'text-slate-300',
};

/**
 * One labelled side of the debate. Empty sections render an explicit muted
 * placeholder instead of collapsing to blank space.
 */
function DebateSection({
  tone,
  label,
  badge,
  text,
  className,
}: {
  tone: DebateTone;
  label: string;
  badge?: ReactNode;
  text: string | null;
  className?: string;
}): ReactElement {
  return (
    <section
      aria-label={label}
      className={cn(
        'flex min-w-0 flex-col rounded-lg border p-2.5 transition-colors duration-300',
        DEBATE_SECTION_CLASS[tone],
        className,
      )}
    >
      <header className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <h3
          className={cn(
            'font-mono text-[10px] font-semibold uppercase tracking-[0.14em]',
            DEBATE_HEADING_CLASS[tone],
          )}
        >
          {label}
        </h3>
        {badge ? <span className="ml-auto">{badge}</span> : null}
      </header>

      {text ? (
        <p className="mt-1.5 max-h-64 overflow-y-auto whitespace-pre-wrap break-words pr-1 text-[11px] leading-relaxed text-slate-300">
          {text}
        </p>
      ) : (
        <p className="mt-1.5 text-[11px] italic text-slate-500">Not available for this decision.</p>
      )}
    </section>
  );
}

/** Bull vs bear columns (stacked on mobile) with the CIO verdict beneath. */
function DebateView({ item, action }: { item: FeedItem; action: ActionDescriptor | null }): ReactElement {
  const bullCase = normalizeDebateText(item.bullCase);
  const bearCase = normalizeDebateText(item.bearCase);
  const verdict = normalizeDebateText(item.reasoning);
  const actionText = normalizeDebateText(item.action);

  return (
    <div className="mt-2 grid animate-fade-in grid-cols-1 gap-2 md:grid-cols-2">
      <DebateSection tone="bull" label="🐂 Bull Case" text={bullCase} />
      <DebateSection tone="bear" label="🐻 Bear Case" text={bearCase} />
      <DebateSection
        tone="verdict"
        label="⚖️ CIO Verdict"
        className="md:col-span-2"
        badge={action ? <Badge tone={action.tone}>{action.label}</Badge> : null}
        text={verdict}
      />

      {actionText ? (
        <p className="md:col-span-2 -mt-0.5 whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-slate-400">
          {actionText}
        </p>
      ) : null}
    </div>
  );
}

const FeedRow = memo(function FeedRow({ item, now }: FeedRowProps): ReactElement {
  const [expanded, setExpanded] = useState(false);
  const isDecision = item.kind === 'decision';
  const body = isDecision ? item.reasoning : item.message;
  const isLong = typeof body === 'string' && body.length > 180;
  const action = isDecision ? deriveAction(item.action) : null;

  const hasDebate =
    normalizeDebateText(item.bullCase) !== null || normalizeDebateText(item.bearCase) !== null;
  const canToggle = isDecision ? isLong || hasDebate || expanded : isLong || expanded;

  return (
    <li className="animate-feed-in rounded-lg border border-slate-800/70 bg-slate-950/40 p-2.5 transition-colors duration-300 hover:border-slate-700/80 hover:bg-slate-900/50">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span
          className="font-mono text-[10px] tabular-nums text-slate-500"
          title={new Date(item.timestamp).toString()}
        >
          {formatClockTime(item.timestamp)}
        </span>
        <span className="font-mono text-[10px] text-slate-600">{formatRelativeTime(item.timestamp, now)}</span>

        {item.ticker ? (
          <span className="font-mono text-xs font-semibold tracking-wide text-sky-300">{item.ticker}</span>
        ) : null}

        {item.kind === 'thinking' ? (
          <Badge tone={item.stage === 'cio_started' || item.stage === 'cio_finished' ? 'violet' : 'sky'}>
            {stageLabel(item.stage)}
          </Badge>
        ) : (
          <>
            {action ? <Badge tone={action.tone}>{action.label}</Badge> : null}
            <Badge tone="slate">decision</Badge>
          </>
        )}

        <SentimentBadge sentiment={item.sentiment} />

        <span className="ml-auto flex items-center gap-2 font-mono text-[10px] tabular-nums text-slate-500">
          {item.tokensUsed !== null ? <span>{formatQuantity(item.tokensUsed, 0)} tok</span> : null}
          {item.apiCostUsd !== null ? <span>{formatCurrency(item.apiCostUsd, { decimals: 5 })}</span> : null}
        </span>
      </div>

      {item.action ? (
        <p className="mt-1.5 font-mono text-xs leading-relaxed text-slate-200">{item.action}</p>
      ) : null}

      {isDecision ? (
        expanded ? (
          <DebateView item={item} action={action} />
        ) : body ? (
          <p className="mt-1.5 line-clamp-3 whitespace-pre-wrap break-words text-[11px] leading-relaxed text-slate-400">
            {body}
          </p>
        ) : null
      ) : body ? (
        <p
          className={cn(
            'mt-1.5 whitespace-pre-wrap break-words text-[11px] leading-relaxed text-slate-400',
            !expanded && 'line-clamp-3',
          )}
        >
          {body}
        </p>
      ) : null}

      {canToggle ? (
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          aria-expanded={expanded}
          className="mt-1 rounded font-mono text-[10px] uppercase tracking-wider text-emerald-400/90 transition-colors hover:text-emerald-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400/60"
        >
          {expanded
            ? isDecision
              ? 'Hide debate'
              : 'Hide reasoning'
            : isDecision
              ? 'Show debate'
              : 'Show reasoning'}
        </button>
      ) : null}
    </li>
  );
});

/** Live AI thought stream: REST log + `decision.created` / `agent.thinking`. */
export function ThoughtsFeed({
  items,
  loading,
  error,
  onRetry,
  cap,
  className,
}: ThoughtsFeedProps): ReactElement {
  const now = useNow(5_000);

  return (
    <Panel
      title="AI Thoughts"
      subtitle={`Newest first · capped at ${cap} entries`}
      actions={
        <span className="inline-flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider text-slate-500">
          <span className="h-1.5 w-1.5 animate-live-pulse rounded-full bg-emerald-400" />
          streaming
        </span>
      }
      bodyClassName="p-3"
      className={className}
    >
      {error ? (
        <ErrorState message={error} onRetry={onRetry} />
      ) : loading && items.length === 0 ? (
        <SkeletonRows rows={4} />
      ) : items.length === 0 ? (
        <EmptyState
          title="No reasoning yet"
          description="The agent's chain-of-thought appears here as soon as it evaluates the market."
        />
      ) : (
        <ul className="max-h-[26rem] space-y-2 overflow-y-auto pr-1">
          {items.map((item) => (
            <FeedRow key={item.key} item={item} now={now} />
          ))}
        </ul>
      )}
    </Panel>
  );
}
