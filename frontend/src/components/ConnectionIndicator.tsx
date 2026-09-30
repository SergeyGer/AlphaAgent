import type { ReactElement } from 'react';
import { cn } from '../lib/cn';
import { formatRelativeTime } from '../lib/format';
import type { ConnectionStatus } from '../types';
import { useNow } from '../hooks/useNow';

export interface ConnectionIndicatorProps {
  status: ConnectionStatus;
  attempt: number;
  nextRetryAt: number | null;
  lastMessageAt: number | null;
  onReconnect: () => void;
}

interface Visual {
  dot: string;
  text: string;
  label: string;
  pulse: boolean;
}

function visualFor(status: ConnectionStatus): Visual {
  switch (status) {
    case 'live':
      return {
        dot: 'bg-emerald-400 shadow-[0_0_10px_2px_rgba(52,211,153,0.6)]',
        text: 'text-emerald-300',
        label: 'LIVE',
        pulse: true,
      };
    case 'connecting':
      return { dot: 'bg-amber-400', text: 'text-amber-300', label: 'CONNECTING', pulse: true };
    case 'reconnecting':
      return { dot: 'bg-amber-400', text: 'text-amber-300', label: 'RECONNECTING', pulse: true };
    case 'offline':
      return { dot: 'bg-rose-500', text: 'text-rose-300', label: 'DISCONNECTED', pulse: false };
    default:
      return { dot: 'bg-slate-500', text: 'text-slate-400', label: 'OFFLINE', pulse: false };
  }
}

/** WebSocket health pill: LIVE pulse, or a countdown while reconnecting. */
export function ConnectionIndicator({
  status,
  attempt,
  nextRetryAt,
  lastMessageAt,
  onReconnect,
}: ConnectionIndicatorProps): ReactElement {
  const now = useNow(1_000);
  const visual = visualFor(status);
  const isLive = status === 'live';

  const retryInSeconds =
    nextRetryAt !== null ? Math.max(0, Math.ceil((nextRetryAt - now) / 1000)) : null;

  const detail = isLive
    ? lastMessageAt
      ? `last frame ${formatRelativeTime(new Date(lastMessageAt).toISOString(), now)}`
      : 'awaiting first frame'
    : retryInSeconds !== null
      ? `retry in ${retryInSeconds}s · attempt ${Math.max(attempt, 1)}`
      : 'socket closed';

  return (
    <div
      className="flex items-center gap-2 rounded-md border border-slate-800 bg-slate-950/60 px-2 py-1"
      title={`WebSocket /ws/portfolio/ — ${detail}`}
    >
      <span className="relative flex h-2 w-2 items-center justify-center">
        <span className={cn('h-2 w-2 rounded-full', visual.dot)} />
        {visual.pulse ? (
          <span className={cn('absolute h-2 w-2 animate-live-pulse rounded-full', visual.dot)} />
        ) : null}
      </span>
      <span className={cn('font-mono text-[10px] font-semibold tracking-[0.14em]', visual.text)}>
        {visual.label}
      </span>
      <span className="hidden font-mono text-[10px] text-slate-500 sm:inline">{detail}</span>
      {!isLive ? (
        <button
          type="button"
          onClick={onReconnect}
          className="rounded border border-slate-700 px-1.5 py-px font-mono text-[10px] uppercase tracking-wider text-slate-300 transition-colors hover:bg-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-slate-500"
        >
          Retry
        </button>
      ) : null}
    </div>
  );
}
