import type { ReactElement, ReactNode } from 'react';
import { cn } from '../lib/cn';
import type { MarketSentiment, RecommendationStatus, TxType } from '../types';

export type BadgeTone = 'emerald' | 'rose' | 'amber' | 'sky' | 'violet' | 'slate';

const TONE_CLASS: Record<BadgeTone, string> = {
  emerald: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300',
  rose: 'border-rose-500/30 bg-rose-500/10 text-rose-300',
  amber: 'border-amber-500/30 bg-amber-500/10 text-amber-300',
  sky: 'border-sky-500/30 bg-sky-500/10 text-sky-300',
  violet: 'border-violet-500/30 bg-violet-500/10 text-violet-300',
  slate: 'border-slate-600/40 bg-slate-700/20 text-slate-300',
};

export interface BadgeProps {
  children: ReactNode;
  tone?: BadgeTone;
  className?: string;
  /** Render in the monospace face (default true — terminal aesthetic). */
  mono?: boolean;
  title?: string;
}

export function Badge({ children, tone = 'slate', className, mono = true, title }: BadgeProps): ReactElement {
  return (
    <span
      title={title}
      className={cn(
        'inline-flex items-center gap-1 whitespace-nowrap rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase leading-4 tracking-wider',
        mono && 'font-mono',
        TONE_CLASS[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

export function SentimentBadge({ sentiment }: { sentiment: MarketSentiment | null }): ReactElement | null {
  if (!sentiment) return null;
  const normalized = sentiment.toUpperCase();
  const tone: BadgeTone =
    normalized === 'BULLISH' ? 'emerald' : normalized === 'BEARISH' ? 'rose' : 'slate';
  return <Badge tone={tone}>{normalized}</Badge>;
}

export function SideBadge({ side }: { side: TxType | string | null }): ReactElement {
  const normalized = (side ?? '').toUpperCase();
  const tone: BadgeTone = normalized === 'BUY' ? 'emerald' : normalized === 'SELL' ? 'rose' : 'slate';
  return <Badge tone={tone}>{normalized || '—'}</Badge>;
}

const STATUS_TONE: Record<RecommendationStatus, BadgeTone> = {
  PENDING: 'amber',
  APPROVED: 'sky',
  REJECTED: 'rose',
  EXPIRED: 'slate',
  EXECUTED: 'emerald',
  BLOCKED: 'violet',
};

export function StatusBadge({ status }: { status: RecommendationStatus | string }): ReactElement {
  const key = (status ?? '').toUpperCase() as RecommendationStatus;
  const tone = STATUS_TONE[key] ?? 'slate';
  return <Badge tone={tone}>{key || '—'}</Badge>;
}

export function ExecutorBadge({ executor }: { executor: string | null }): ReactElement {
  const normalized = (executor ?? '').toUpperCase();
  const tone: BadgeTone = normalized === 'AI' ? 'violet' : normalized === 'USER' ? 'sky' : 'slate';
  return <Badge tone={tone}>{normalized || '—'}</Badge>;
}
