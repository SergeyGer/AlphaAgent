import type { ReactElement } from 'react';
import type { ThesisRow } from '../types';
import { Skeleton } from './Feedback';

/**
 * The recorded reason for holding each open position, and what would falsify it.
 *
 * This is not a summary of the last decision. It is the standing claim the agents
 * are given on every run, so showing it lets a reader check the system's stated
 * belief against its behaviour — and notice when it exits without saying what
 * changed.
 *
 * A missing invalidation is stated rather than left blank. An agent that cannot
 * name a condition that would prove it wrong has said something worth seeing, and
 * an empty line would read as a rendering fault instead.
 */

export interface PositionThesesProps {
  theses: ThesisRow[] | null;
  loading: boolean;
}

function ThesisCard({ row }: { row: ThesisRow }): ReactElement {
  const invalidation = (row.invalidation ?? '').trim();

  return (
    <li className="rounded-lg border border-slate-800/70 bg-slate-900/40 px-3 py-2.5">
      <div className="mb-1.5 flex items-baseline gap-2">
        <span className="font-mono text-xs font-semibold tracking-wide text-slate-100">
          {row.ticker}
        </span>
        <span className="font-mono text-[9px] uppercase tracking-wider text-emerald-300/80">
          held
        </span>
      </div>

      <p className="text-xs leading-relaxed text-slate-300">{row.thesis}</p>

      {invalidation ? (
        <p className="mt-2 text-[11px] leading-relaxed text-amber-200/80">
          <span className="font-mono text-[9px] uppercase tracking-wider text-amber-300/70">
            would be wrong if ·{' '}
          </span>
          {invalidation}
        </p>
      ) : (
        <p className="mt-2 font-mono text-[10px] uppercase tracking-wider text-slate-500">
          no invalidation condition recorded
        </p>
      )}
    </li>
  );
}

export function PositionTheses({ theses, loading }: PositionThesesProps): ReactElement | null {
  if (loading && !theses) {
    return (
      <div className="rounded-lg border border-slate-800/70 bg-slate-900/30 px-3 py-3">
        <Skeleton className="h-3 w-40" />
        <Skeleton className="mt-2 h-3 w-64" />
      </div>
    );
  }

  // Nothing to show is a real state, not an error: a position can be opened
  // before a thesis exists, or by an operator-approved trade. Rendering an empty
  // shell would imply the panel was broken.
  if (!theses?.length) return null;

  return (
    <section
      aria-label="Position theses"
      className="rounded-lg border border-slate-800/70 bg-slate-900/30 px-3 py-2.5"
    >
      <h3 className="mb-2 font-mono text-[10px] uppercase tracking-wider text-slate-500">
        Position theses · {theses.length} open
      </h3>
      <ul className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
        {theses.map((row) => (
          <ThesisCard key={row.id} row={row} />
        ))}
      </ul>
    </section>
  );
}
