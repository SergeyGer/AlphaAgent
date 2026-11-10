import { useId, type ReactElement } from 'react';
import { cn } from '../lib/cn';
import {
  buildTimeline,
  eventsUpTo,
  formatRunTimestamp,
  type TimelineNode,
} from '../lib/decisionRuns';
import type { RunEvent } from '../types';

/**
 * Stage-level replay timeline.
 *
 * The backend may emit one event per token; those are aggregated into the stage
 * they belong to, so a reader sees six pipeline steps rather than six hundred
 * deltas. When `hasRebuttals` is false no rebuttal node is rendered at all: the
 * legacy pipeline had no such stage, and an empty "Rebuttals" section would
 * imply it ran and produced nothing.
 */

export interface DebateTimelineProps {
  events: RunEvent[];
  hasRebuttals: boolean;
  /**
   * Replay cursor. `undefined` shows every event, `null` shows none (the start
   * of a replay), a number shows every event up to and including that sequence.
   */
  cursorSeq?: number | null;
  className?: string;
}

function agentLabel(role: string): string {
  const key = role.trim().toLowerCase();
  if (key === 'bull') return 'Bull';
  if (key === 'bear') return 'Bear';
  return role.charAt(0).toUpperCase() + role.slice(1);
}

function TimelineEntry({
  node,
  index,
  isCurrent,
}: {
  node: TimelineNode;
  index: number;
  isCurrent: boolean;
}): ReactElement {
  const timestamp = formatRunTimestamp(node.occurredAt);
  const agents = node.agents.map(agentLabel);

  return (
    <li
      data-stage={node.stage}
      data-current={isCurrent ? 'true' : undefined}
      className="relative min-w-0 pl-5"
    >
      <span
        aria-hidden="true"
        className={cn(
          'absolute left-0 top-2.5 h-2 w-2 -translate-x-[0.3rem] rounded-full ring-2 ring-slate-950',
          node.failed ? 'bg-rose-400' : isCurrent ? 'bg-cyan-300' : 'bg-slate-600',
        )}
      />
      <article
        aria-label={`Stage ${index + 1}: ${node.label}`}
        className={cn(
          'min-w-0 rounded-lg border px-3 py-2 transition-colors',
          isCurrent
            ? 'border-cyan-500/40 bg-cyan-500/[0.06]'
            : 'border-slate-800/70 bg-slate-950/40',
        )}
      >
        <header className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <h3 className="text-sm font-semibold text-slate-100">{node.label}</h3>
          {node.failed ? (
            <span className="inline-flex rounded border border-rose-500/30 bg-rose-500/10 px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider text-rose-300">
              Failed
            </span>
          ) : null}
          {isCurrent ? (
            <span className="inline-flex rounded border border-cyan-500/30 bg-cyan-500/10 px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider text-cyan-200">
              Current
            </span>
          ) : null}
          <span className="ml-auto font-mono text-[10px] uppercase tracking-wider text-slate-400">
            seq {node.firstSeq}
            {node.lastSeq !== node.firstSeq ? `–${node.lastSeq}` : ''}
          </span>
        </header>

        <p className="mt-0.5 flex flex-wrap gap-x-2 gap-y-0.5 font-mono text-[11px] text-slate-400">
          <span>{timestamp === 'Unknown' ? 'Time unknown' : timestamp}</span>
          <span aria-hidden="true">·</span>
          <span>
            {node.eventCount} stage event{node.eventCount === 1 ? '' : 's'}
          </span>
          {agents.length > 0 ? (
            <>
              <span aria-hidden="true">·</span>
              <span>{agents.join(', ')}</span>
            </>
          ) : null}
        </p>

        {node.summary ? (
          <p className="mt-1 whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-300">
            {node.summary}
          </p>
        ) : null}
      </article>
    </li>
  );
}

export function DebateTimeline({
  events,
  hasRebuttals,
  cursorSeq,
  className,
}: DebateTimelineProps): ReactElement {
  const titleId = useId();
  const sourceEvents = Array.isArray(events) ? events.filter(Boolean) : [];
  const visibleEvents = eventsUpTo(sourceEvents, cursorSeq);
  const nodes = buildTimeline(visibleEvents, Boolean(hasRebuttals));
  const atStart = cursorSeq === null && sourceEvents.length > 0;
  const currentKey = nodes.length > 0 ? nodes[nodes.length - 1]?.key : null;

  return (
    <section
      aria-labelledby={titleId}
      className={cn(
        'min-w-0 rounded-xl border border-slate-800/80 bg-slate-900/50 p-3 shadow-panel sm:p-4',
        className,
      )}
    >
      <header className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 border-b border-slate-800/70 pb-2">
        <div className="min-w-0">
          <h2
            id={titleId}
            className="font-mono text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-400"
          >
            Debate timeline
          </h2>
          <p className="mt-0.5 text-sm text-slate-400">
            Stage-level events only — token streams are collapsed into their pipeline step.
          </p>
        </div>
        <span className="font-mono text-[10px] uppercase tracking-wider text-slate-400">
          {nodes.length} stage{nodes.length === 1 ? '' : 's'}
        </span>
      </header>

      {atStart ? (
        <div data-state="replay-start" className="mt-3 rounded-lg border border-dashed border-slate-800 bg-slate-950/40 px-4 py-4">
          <p className="text-sm leading-relaxed text-slate-400">
            Replay is at the start — no stage events are shown yet. Press play, step forward, or
            choose a stage.
          </p>
        </div>
      ) : nodes.length === 0 ? (
        <div data-state="timeline-empty" className="mt-3 rounded-lg border border-dashed border-slate-800 bg-slate-950/40 px-4 py-4">
          <p className="text-sm leading-relaxed text-slate-400">
            No stage events were recorded for this run. The timeline is not reconstructed from the
            final report.
          </p>
        </div>
      ) : (
        <ol aria-label="Decision timeline" className="relative mt-3 space-y-2 border-l border-slate-800">
          {nodes.map((node, index) => (
            <TimelineEntry
              key={node.key}
              node={node}
              index={index}
              isCurrent={node.key === currentKey}
            />
          ))}
        </ol>
      )}
    </section>
  );
}
