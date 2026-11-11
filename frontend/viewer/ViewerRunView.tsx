import { useEffect, useId, useState, type ReactElement } from 'react';
import { cn } from '../src/lib/cn';
import {
  buildAsOfPresentation,
  decisionSummary,
  lastEventSequence,
  runModeLabel,
  type ReplayTimingMode,
} from '../src/lib/decisionRuns';
import { Badge, type BadgeTone } from '../src/components/Badge';
import { DebateTimeline } from '../src/components/DebateTimeline';
import { DecisionDetail } from '../src/components/DecisionDetail';
import { ReplayControls } from '../src/components/ReplayControls';
import {
  SOURCE_LABEL_DESCRIPTION,
  SOURCE_LABEL_TEXT,
  sourceLabelForRun,
  type ViewerRun,
} from './bundle';

/**
 * One stored run inside the static viewer.
 *
 * This is deliberately the same `DecisionDetail`, `DebateTimeline` and
 * `ReplayControls` the authenticated application uses. The viewer supplies the
 * as-of reduction built by `buildAsOfPresentation`; it does not fork the
 * presentation rules. The only viewer-specific choices are the provenance
 * banner, the absence of any mutation control, and external source links being
 * rendered as non-clickable text.
 */

export interface ViewerRunViewProps {
  run: ViewerRun;
  className?: string;
}

const SOURCE_TONE: Record<string, BadgeTone> = {
  real_recorded: 'emerald',
  synthetic: 'violet',
  heuristic: 'amber',
  legacy: 'slate',
};

export function ViewerRunView({ run, className }: ViewerRunViewProps): ReactElement {
  const headingId = useId();
  const source = sourceLabelForRun(run);
  const maxSeq = lastEventSequence(run.events);
  const [cursorSeq, setCursorSeq] = useState<number | null>(maxSeq);
  const [timing, setTiming] = useState<ReplayTimingMode>('compressed');

  // A different imported run must not inherit the previous cursor.
  useEffect(() => {
    setCursorSeq(lastEventSequence(run.events));
  }, [run.run_id, run.events]);

  const presentation = buildAsOfPresentation(run.detail, run.events, cursorSeq);
  const atEnd = presentation?.atEnd ?? true;
  const pinnedFinalSummary = presentation && !atEnd ? decisionSummary(run.detail) : null;
  const scene = presentation
    ? {
        active: true,
        atEnd: presentation.atEnd,
        cursorSeq: presentation.cursorSeq,
        verdictVisible: presentation.verdictVisible,
        guardVisible: presentation.guardVisible,
        outcomesVisible: presentation.outcomesVisible,
        hiddenClaims: presentation.hidden.claims,
        hiddenEvidence: presentation.hidden.evidence,
        hiddenOutcomes: presentation.hidden.outcomes,
        hiddenRebuttals: presentation.hidden.rebuttals,
      }
    : undefined;

  return (
    <section aria-labelledby={headingId} className={cn('min-w-0 space-y-3', className)}>
      <header className="min-w-0 rounded-xl border border-slate-800/80 bg-slate-900/50 p-3 shadow-panel sm:p-4">
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
          <div className="min-w-0">
            <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
              Offline decision replay
            </p>
            <h2
              id={headingId}
              className="mt-0.5 break-words font-mono text-xl font-semibold tracking-tight text-slate-100"
            >
              {run.detail.run.ticker.toUpperCase()}
            </h2>
            <p className="mt-0.5 break-all font-mono text-[11px] text-slate-400">
              {run.run_id}
            </p>
          </div>
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <span
              data-testid="viewer-source-label"
              data-source-label={source}
              className="inline-flex items-center gap-1.5"
            >
              <span className="font-mono text-[10px] uppercase tracking-wider text-slate-400">
                Source
              </span>
              <Badge tone={SOURCE_TONE[source] ?? 'slate'}>{SOURCE_LABEL_TEXT[source]}</Badge>
            </span>
            <Badge tone="sky">{runModeLabel(run.mode)}</Badge>
            <Badge tone="slate">{run.events.length} stored events</Badge>
          </div>
        </div>

        <p className="mt-2 max-w-4xl text-sm leading-relaxed text-slate-300" data-testid="viewer-provenance">
          {SOURCE_LABEL_DESCRIPTION[source]}
        </p>
        <p className="mt-1 break-words text-xs leading-relaxed text-slate-400">
          {run.provenance.note}
        </p>
        {run.provenance.fixture_id ? (
          <p className="mt-1 break-all font-mono text-[10px] uppercase tracking-wider text-slate-500">
            Fixture {run.provenance.fixture_id} · protocol{' '}
            {run.provenance.protocol_version ?? 'not recorded'}
          </p>
        ) : null}
        <p className="mt-2 rounded-md border border-emerald-500/25 bg-emerald-500/[0.05] px-2.5 py-1.5 text-xs leading-relaxed text-emerald-200/90">
          Read-only replay. No execution, approval, model call or live-analysis capability is
          compiled into this page.
        </p>
      </header>

      {pinnedFinalSummary ? (
        <details
          data-testid="viewer-pinned-final-summary"
          className="rounded-xl border border-amber-500/30 bg-amber-500/[0.06] px-4 py-3"
        >
          <summary className="cursor-pointer font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-amber-200">
            Final summary (pinned — not part of the replay scene)
          </summary>
          <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-200">
            {pinnedFinalSummary}
          </p>
          <p className="mt-1 text-xs leading-relaxed text-amber-200/80">
            The stored final summary is deliberately outside the replay scene and may describe
            events later than the current cursor.
          </p>
        </details>
      ) : null}

      <DecisionDetail
        detail={presentation?.detail ?? run.detail}
        loading={false}
        error={null}
        onRetry={() => undefined}
        scene={scene}
        allowExternalLinks={false}
      />

      <div className="space-y-3">
        {run.events.length > 0 ? (
          <ReplayControls
            events={run.events}
            hasRebuttals={run.detail.has_rebuttals}
            cursorSeq={cursorSeq}
            onCursorSeqChange={setCursorSeq}
            timingMode={timing}
            onTimingModeChange={(mode) => {
              setTiming(mode);
              setCursorSeq((current) => current);
            }}
          />
        ) : (
          <p
            data-state="viewer-no-events"
            className="rounded-xl border border-dashed border-slate-800 bg-slate-950/40 px-4 py-3 text-sm text-slate-400"
          >
            This bundle contains no stored events. Nothing is simulated in their place.
          </p>
        )}

        <DebateTimeline
          events={run.events}
          hasRebuttals={run.detail.has_rebuttals}
          cursorSeq={cursorSeq}
        />
      </div>
    </section>
  );
}
