import { useCallback, useEffect, useId, useMemo, useState, type ReactElement } from 'react';
import { cn } from '../lib/cn';
import {
  buildTimeline,
  eventSequences,
  isAtReplayEnd,
  stageAtCursor,
  stepReplayCursor,
  type TimelineNode,
} from '../lib/decisionRuns';
import type { RunEvent } from '../types';
import { Badge } from './Badge';

/**
 * REP-01 — deterministic replay of a stored run.
 *
 * State is a single sequence number, not an accumulated buffer: what is visible
 * at sequence N is always the same set of events, so stepping forward, backward
 * and forward again cannot drift. The "Recorded run" label is part of the
 * controls, not an overlay that disappears while playing.
 *
 * Keyboard: every control is a native button, reachable with Tab and activated
 * with Enter/Space. Stage changes are announced once through a polite live
 * region; individual events within a stage are not read aloud.
 */

export interface ReplayControlsProps {
  events: RunEvent[];
  hasRebuttals?: boolean;
  cursorSeq: number | null;
  onCursorSeqChange: (seq: number | null) => void;
  disabled?: boolean;
  className?: string;
}

const SPEEDS = [0.5, 1, 2] as const;
type PlaybackSpeed = (typeof SPEEDS)[number];

const CONTROL_CLASS =
  'inline-flex min-h-[36px] items-center justify-center gap-1.5 rounded-md border border-slate-700 bg-slate-950/70 px-2.5 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-wider text-slate-200 transition-colors hover:bg-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950 disabled:cursor-not-allowed disabled:opacity-40';

const SPEED_CLASS =
  'min-h-[32px] rounded-md border px-2 py-1 font-mono text-[11px] font-semibold uppercase tracking-wider transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950';

function StageButton({
  node,
  isActive,
  onSelect,
}: {
  node: TimelineNode;
  isActive: boolean;
  onSelect: (seq: number) => void;
}): ReactElement {
  return (
    <button
      type="button"
      onClick={() => onSelect(node.lastSeq)}
      aria-pressed={isActive}
      aria-label={`Jump to stage ${node.label}`}
      className={cn(
        'min-h-[32px] rounded-md border px-2 py-1 text-left text-sm transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950',
        isActive
          ? 'border-cyan-500/50 bg-cyan-500/10 text-cyan-100'
          : 'border-slate-700 bg-slate-950/50 text-slate-300 hover:bg-slate-800',
      )}
    >
      {node.label}
    </button>
  );
}

export function ReplayControls({
  events,
  hasRebuttals = true,
  cursorSeq,
  onCursorSeqChange,
  disabled = false,
  className,
}: ReplayControlsProps): ReactElement {
  const headingId = useId();
  const sourceEvents = useMemo(
    () => (Array.isArray(events) ? events.filter(Boolean) : []),
    [events],
  );
  const sequences = useMemo(() => eventSequences(sourceEvents), [sourceEvents]);
  const stages = useMemo(
    () => buildTimeline(sourceEvents, Boolean(hasRebuttals)),
    [hasRebuttals, sourceEvents],
  );
  const activeStage = useMemo(
    () => stageAtCursor(sourceEvents, cursorSeq, Boolean(hasRebuttals)),
    [cursorSeq, hasRebuttals, sourceEvents],
  );

  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<PlaybackSpeed>(1);

  const atStart = cursorSeq === null;
  const atEnd = isAtReplayEnd(sourceEvents, cursorSeq);
  const noEvents = sequences.length === 0 || disabled;
  const cursorCount =
    cursorSeq === null ? 0 : sequences.filter((seq) => seq <= cursorSeq).length;

  const step = useCallback(
    (direction: 'previous' | 'next') => {
      setPlaying(false);
      onCursorSeqChange(stepReplayCursor(sourceEvents, cursorSeq, direction));
    },
    [cursorSeq, onCursorSeqChange, sourceEvents],
  );

  const restart = useCallback(() => {
    setPlaying(false);
    onCursorSeqChange(null);
  }, [onCursorSeqChange]);

  const handlePlayPause = useCallback(() => {
    if (playing) {
      setPlaying(false);
      return;
    }
    if (noEvents) return;

    if (cursorSeq === null || atEnd) {
      // Starting from the beginning gives immediate feedback instead of a
      // silent wait for the first tick. Clicking play at the end restarts.
      onCursorSeqChange(sequences[0] ?? null);
    }
    setPlaying(true);
  }, [atEnd, cursorSeq, noEvents, onCursorSeqChange, playing, sequences]);

  const selectStage = useCallback(
    (seq: number) => {
      setPlaying(false);
      onCursorSeqChange(seq);
    },
    [onCursorSeqChange],
  );

  // A single pending timeout per step. Re-created when the cursor, speed or
  // event list changes, so pause takes effect immediately.
  useEffect(() => {
    if (!playing || noEvents) return undefined;
    if (atEnd) {
      setPlaying(false);
      return undefined;
    }

    const delay = Math.max(120, Math.round(800 / speed));
    const timer = window.setTimeout(() => {
      onCursorSeqChange(stepReplayCursor(sourceEvents, cursorSeq, 'next'));
    }, delay);
    return () => window.clearTimeout(timer);
  }, [atEnd, cursorSeq, noEvents, onCursorSeqChange, playing, sourceEvents, speed]);

  // A new run (or an emptied event list) must never keep playing invisibly.
  useEffect(() => {
    if (noEvents) setPlaying(false);
  }, [noEvents]);

  const announcement =
    cursorSeq === null
      ? 'Replay at start.'
      : activeStage
        ? `Stage ${activeStage.label}.`
        : 'Replay at an unknown stage.';

  return (
    <section
      aria-labelledby={headingId}
      className={cn(
        'min-w-0 rounded-xl border border-slate-800/80 bg-slate-900/50 p-3 shadow-panel sm:p-4',
        className,
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-2 border-b border-slate-800/70 pb-2">
        <div className="min-w-0">
          <h2
            id={headingId}
            className="font-mono text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-400"
          >
            Replay
          </h2>
          <p className="mt-0.5 text-sm text-slate-400">
            Deterministic playback of the stored event log.
          </p>
        </div>
        <span className="inline-flex items-center" data-testid="recorded-run-label">
          <Badge tone="sky">Recorded run</Badge>
        </span>
      </div>

      <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {announcement}
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={handlePlayPause}
          disabled={noEvents}
          aria-label={playing ? 'Pause replay' : 'Play replay'}
          className={CONTROL_CLASS}
        >
          <span aria-hidden="true">{playing ? '❚❚' : '▶'}</span>
          {playing ? 'Pause' : 'Play'}
        </button>

        <button
          type="button"
          onClick={() => step('previous')}
          disabled={noEvents || atStart}
          aria-label="Previous event"
          className={CONTROL_CLASS}
        >
          <span aria-hidden="true">◀</span>
          Previous
        </button>

        <button
          type="button"
          onClick={() => step('next')}
          disabled={noEvents || atEnd}
          aria-label="Next event"
          className={CONTROL_CLASS}
        >
          Next
          <span aria-hidden="true">▶</span>
        </button>

        <button
          type="button"
          onClick={restart}
          disabled={noEvents || atStart}
          aria-label="Restart replay"
          className={CONTROL_CLASS}
        >
          <span aria-hidden="true">↺</span>
          Restart
        </button>

        <div
          role="group"
          aria-label="Playback speed"
          className="ml-auto inline-flex items-center gap-1 rounded-md border border-slate-800 p-1"
        >
          {SPEEDS.map((option) => (
            <button
              key={option}
              type="button"
              onClick={() => setSpeed(option)}
              aria-pressed={speed === option}
              aria-label={`Playback speed ${option}x`}
              className={cn(
                SPEED_CLASS,
                speed === option
                  ? 'border-cyan-500/40 bg-cyan-500/10 text-cyan-100'
                  : 'border-transparent text-slate-400 hover:bg-slate-800 hover:text-slate-200',
              )}
            >
              {option}x
            </button>
          ))}
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center justify-between gap-x-3 gap-y-1 font-mono text-[11px] tabular-nums text-slate-400">
        <p>
          Event {cursorCount} of {sequences.length}
          {activeStage ? ` · ${activeStage.label}` : ' · at start'}
        </p>
        <p className="text-slate-400">
          {atEnd ? 'End of recorded events' : 'Space activates the focused button'}
        </p>
      </div>

      {stages.length > 0 ? (
        <div role="group" aria-label="Jump to stage" className="mt-3 border-t border-slate-800/70 pt-3">
          <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
            Jump to stage
          </p>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {stages.map((node) => (
              <StageButton
                key={node.key}
                node={node}
                isActive={activeStage?.key === node.key}
                onSelect={selectStage}
              />
            ))}
          </div>
        </div>
      ) : null}

      {noEvents && !disabled ? (
        <p className="mt-3 rounded-md border border-dashed border-slate-800 bg-slate-950/40 px-3 py-2 text-sm text-slate-400">
          No recorded events to play. Nothing is simulated in their place.
        </p>
      ) : null}
    </section>
  );
}
