import { useEffect, useMemo, useRef, type ReactElement } from 'react';
import { cn } from '../lib/cn';
import {
  buildAsOfPresentation,
  decisionSummary,
  eventsRequiringDetailRefresh,
  formatLatency,
  formatRunTimestamp,
  isActiveRunStatus,
  isTerminalRunStatus,
  lastEventSequence,
  presentText,
  runModeLabel,
  runStatusLabel,
} from '../lib/decisionRuns';
import { useAuth } from '../hooks/useAuth';
import {
  useDecisionRun,
  useDecisionRunEvents,
  useDecisionRuns,
} from '../hooks/useDecisionRuns';
import { useReplayNavigation } from '../hooks/useReplayNavigation';
import type { RunSummary } from '../types';
import { Badge, type BadgeTone } from './Badge';
import { DebateTimeline } from './DebateTimeline';
import { DecisionDetail, type DecisionDetailScene } from './DecisionDetail';
import { ErrorState, SkeletonRows } from './Feedback';
import { Panel } from './Panel';
import { ReplayControls } from './ReplayControls';

/**
 * Route entry point for UX-01 / REP-01.
 *
 * Reached at `#/runs` (list) and `#/runs/<id>` (detail + replay). Deliberately
 * a separate top-level view rather than a new dashboard panel: the existing
 * dashboard layout is unchanged, and a reviewer can link directly to one run.
 */

const CONTROL_CLASS =
  'inline-flex min-h-[36px] items-center rounded-md border border-slate-700 bg-slate-950/60 px-2.5 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-wider text-slate-200 transition-colors hover:bg-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950 disabled:cursor-not-allowed disabled:opacity-50';

const MODE_TONE: Record<string, BadgeTone> = {
  live: 'emerald',
  recorded: 'sky',
  heuristic: 'amber',
  legacy: 'slate',
  synthetic: 'violet',
};

const STATUS_TONE: Record<string, BadgeTone> = {
  queued: 'slate',
  collecting: 'sky',
  analysing: 'sky',
  rebutting: 'violet',
  adjudicating: 'violet',
  validating: 'amber',
  awaiting_approval: 'amber',
  completed: 'emerald',
  failed: 'rose',
  cancelled: 'slate',
};

const LINK_CLASS =
  'rounded text-cyan-300 underline decoration-cyan-500/40 underline-offset-2 transition-colors hover:text-cyan-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950';

function modeBadgeTone(mode: string | null | undefined): BadgeTone {
  return MODE_TONE[(mode ?? '').trim().toLowerCase()] ?? 'slate';
}

function statusBadgeTone(status: string | null | undefined): BadgeTone {
  return STATUS_TONE[(status ?? '').trim().toLowerCase()] ?? 'slate';
}

function RunRow({ run }: { run: RunSummary }): ReactElement {
  const id = presentText(run?.id);
  const ticker = presentText(run?.ticker)?.toUpperCase() ?? 'Unknown ticker';
  const started = formatRunTimestamp(run?.started_at);
  const latency = formatLatency(run?.latency_ms);

  const body = (
    <>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm font-semibold tracking-wide text-slate-100">
          {ticker}
        </span>
        <Badge tone={modeBadgeTone(run?.mode)}>{runModeLabel(run?.mode)}</Badge>
        <Badge tone={statusBadgeTone(run?.status)}>{runStatusLabel(run?.status)}</Badge>
        <span className="ml-auto font-mono text-[11px] text-slate-400">
          {started === 'Unknown' ? 'Start time unknown' : started}
        </span>
      </div>
      <p className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[11px] text-slate-400">
        <span>{latency === 'Unknown' ? 'Latency unknown' : `Latency ${latency}`}</span>
        <span>Protocol {presentText(run?.protocol_version) ?? 'version unknown'}</span>
        {presentText(run?.error_code) ? (
          <span className="break-all text-rose-300">Error: {run.error_code}</span>
        ) : null}
      </p>
    </>
  );

  const className = cn(
    'block min-w-0 rounded-lg border border-slate-800/70 bg-slate-950/40 px-3 py-2.5 transition-colors',
    id
      ? 'hover:border-slate-700 hover:bg-slate-900/50 focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950'
      : '',
  );

  return (
    <li className="min-w-0">
      {id ? (
        <a href={`#/runs/${encodeURIComponent(id)}`} className={className}>
          {body}
        </a>
      ) : (
        <div className={className}>{body}</div>
      )}
    </li>
  );
}

function DecisionRunList(): ReactElement {
  const { token } = useAuth();
  const { data, loading, error, refresh } = useDecisionRuns(token);
  const runs = data ?? [];

  return (
    <Panel
      title="Recent decision runs"
      subtitle="One row per stored run. Open a run for claims, evidence, guard checks and outcomes."
      actions={
        <button
          type="button"
          onClick={refresh}
          disabled={loading}
          aria-label="Refresh decision runs"
          className={CONTROL_CLASS}
        >
          Refresh
        </button>
      }
      bodyClassName="p-3 sm:p-4"
    >
      {error && runs.length === 0 ? (
        <ErrorState message={`Could not load decision runs — ${error}`} onRetry={refresh} />
      ) : loading && runs.length === 0 ? (
        <SkeletonRows rows={5} />
      ) : runs.length === 0 ? (
        <div className="rounded-lg border border-dashed border-slate-800 bg-slate-950/40 px-4 py-6">
          <p className="font-mono text-xs uppercase tracking-wider text-slate-400">
            No decision runs recorded
          </p>
          <p className="mt-1 text-sm leading-relaxed text-slate-400">
            Runs appear here once the backend stores a claim/evidence record. Nothing is
            back-filled from the legacy decision log.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {error ? <ErrorState compact message={`Could not refresh — ${error}`} onRetry={refresh} /> : null}
          <ul aria-label="Decision runs" className="space-y-2">
            {runs.map((run, index) => (
              <RunRow key={presentText(run?.id) ?? `run-${index}`} run={run} />
            ))}
          </ul>
        </div>
      )}
    </Panel>
  );
}

export function DecisionRunView({ runId }: { runId: string }): ReactElement {
  const { token } = useAuth();
  const detail = useDecisionRun(runId, token);
  const run = detail.data?.run ?? null;
  const active = run !== null && isActiveRunStatus(run.status);
  // A completed run can still be waiting on a later horizon. The detail view
  // keeps polling for the recorded result until the backend says no measurement
  // work remains; a completed execution state is not the same thing as a
  // finished measurement.
  const schedulePending = detail.data?.outcome_schedule?.pending === true;
  const pendingOutcome =
    schedulePending ||
    (detail.data?.outcomes ?? []).some(
      (outcome) => outcome?.maturity === 'pending' || outcome?.maturity === 'matured',
    );
  const events = useDecisionRunEvents(runId, token, {
    enabled: token !== null,
    pollIntervalMs: active || pendingOutcome ? 2_000 : 0,
  });
  const hasRebuttals = detail.data?.has_rebuttals ?? true;
  const navigation = useReplayNavigation(runId);

  // While a gap is being recovered, the last *contiguous* event is the known
  // cursor. Jumping to the highest held sequence would step past events the
  // client has not received yet.
  const maxSeq = events.catchingUp
    ? events.contiguousSequence
    : lastEventSequence(events.events);
  const replayable =
    events.events.length > 0 && run !== null && isTerminalRunStatus(run.status);

  // -- Live refresh ------------------------------------------------------
  // A persisted stage, claim, rebuttal, decision, guard or outcome changes the
  // detail payload. The events hook is the liveness signal; the detail resource
  // is refreshed when one of those events arrives, so an open run moves from
  // "analysing" to a terminal replay without the reader reloading the page.
  const detailInitialised = useRef(false);
  const lastDetailRefreshSeq = useRef(0);
  useEffect(() => {
    if (!detail.data) return undefined;
    if (!detailInitialised.current) {
      detailInitialised.current = true;
      // The known cursor is the last contiguous event, not the highest one: a
      // gap must be recovered and refreshed, not stepped over.
      lastDetailRefreshSeq.current =
        events.contiguousSequence ?? lastEventSequence(events.events) ?? 0;
      // One reconciliation refresh covers the race between the initial detail
      // request and the first event page. After that, only new events trigger
      // a refresh, so a completed run is not polled forever.
      const initial = window.setTimeout(() => detail.refresh(), 150);
      return () => window.clearTimeout(initial);
    }

    const fresh = eventsRequiringDetailRefresh(events.events, lastDetailRefreshSeq.current);
    if (fresh.length === 0) return undefined;
    lastDetailRefreshSeq.current =
      events.contiguousSequence ?? lastEventSequence(events.events) ?? lastDetailRefreshSeq.current;
    const timer = window.setTimeout(() => detail.refresh(), 120);
    return () => window.clearTimeout(timer);
  }, [detail.data, detail.refresh, events.events]);

  // -- Cursor navigation -------------------------------------------------
  // The URL is the source of truth once a cursor exists. Without one, a stored
  // run opens at its tail; as new events arrive while the reader is already at
  // the tail, follow them. A reader who stepped back is never yanked forward.
  const rawCursor = navigation.hasCursor ? navigation.cursorSeq : maxSeq;
  const cursorSeq =
    rawCursor !== null && rawCursor !== undefined && maxSeq !== null && rawCursor > maxSeq
      ? maxSeq
      : (rawCursor ?? null);
  const lastMaxSeq = useRef<number | null>(null);

  useEffect(() => {
    if (maxSeq === null) return;
    const previousMax = lastMaxSeq.current;
    lastMaxSeq.current = maxSeq;

    if (!navigation.hasCursor) {
      navigation.setCursor(maxSeq, { history: 'replace' });
      return;
    }
    if (navigation.cursorSeq !== null && navigation.cursorSeq !== undefined && navigation.cursorSeq > maxSeq) {
      navigation.setCursor(maxSeq, { history: 'replace' });
      return;
    }
    if (previousMax !== null && navigation.cursorSeq === previousMax && maxSeq > previousMax) {
      navigation.setCursor(maxSeq, { history: 'replace' });
    }
  }, [maxSeq, navigation.hasCursor, navigation.cursorSeq, navigation.setCursor]);

  const presentation = useMemo(
    () =>
      replayable
        ? buildAsOfPresentation(detail.data, events.events, cursorSeq)
        : null,
    [cursorSeq, detail.data, events.events, replayable],
  );

  const scene: DecisionDetailScene | undefined = presentation
    ? {
        active: true,
        atEnd: presentation.atEnd,
        cursorSeq: presentation.cursorSeq,
        verdictVisible: presentation.verdictVisible,
        guardVisible: presentation.guardVisible,
        executionVisible: presentation.executionVisible,
        guardPartial: presentation.guardPartial,
        outcomesVisible: presentation.outcomesVisible,
        hiddenClaims: presentation.hidden.claims,
        hiddenEvidence: presentation.hidden.evidence,
        hiddenOutcomes: presentation.hidden.outcomes,
        hiddenRebuttals: presentation.hidden.rebuttals,
      }
    : undefined;

  const pinnedFinalSummary =
    presentation && !presentation.atEnd ? decisionSummary(detail.data) : null;

  return (
    <div className="space-y-3">
      <nav aria-label="Breadcrumb" className="flex flex-wrap items-center gap-2 text-sm">
        <a href="#/runs" className={LINK_CLASS}>
          ← All decision runs
        </a>
        <span aria-hidden="true" className="text-slate-600">
          /
        </span>
        <span className="break-all font-mono text-xs text-slate-400">{runId}</span>
      </nav>

      {pinnedFinalSummary ? (
        <details
          data-testid="pinned-final-summary"
          className="rounded-xl border border-amber-500/30 bg-amber-500/[0.06] px-4 py-3"
        >
          <summary className="cursor-pointer font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-amber-200">
            Final summary (pinned — not part of the replay scene)
          </summary>
          <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-200">
            {pinnedFinalSummary}
          </p>
          <p className="mt-1 text-xs leading-relaxed text-amber-200/80">
            This is the stored final summary. It is deliberately outside the replay scene and may
            describe events later than the current cursor.
          </p>
        </details>
      ) : null}

      <DecisionDetail
        detail={presentation?.detail ?? detail.data}
        loading={detail.loading}
        error={detail.error}
        onRetry={detail.refresh}
        scene={scene}
      />

      <section aria-label="Run timeline and replay" className="min-w-0 space-y-3">
        {events.error ? (
          <ErrorState
            compact
            message={`Could not load this run's events — ${events.error}`}
            onRetry={events.refresh}
          />
        ) : null}

        {events.loading && events.events.length === 0 ? (
          <p role="status" className="px-1 text-sm text-slate-400">
            Loading recorded events…
          </p>
        ) : null}

        {events.catchingUp || events.remaining > 0 ? (
          <div
            data-state="event-stream-catching-up"
            role="status"
            className="rounded-xl border border-amber-500/30 bg-amber-500/[0.06] px-4 py-3"
          >
            <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-amber-200">
              Event stream catching up
            </p>
            <p className="mt-1 text-sm leading-relaxed text-slate-300">
              {events.remaining} event{events.remaining === 1 ? '' : 's'} up to the server tail
              {events.serverLastSequence !== null ? ` (sequence ${events.serverLastSequence})` : ''}{' '}
              {events.remaining === 1 ? 'is' : 'are'} still not held locally. Playback resumes from
              the last contiguous sequence
              {events.contiguousSequence !== null ? ` (${events.contiguousSequence})` : ''} rather
              than skipping ahead.
            </p>
          </div>
        ) : null}

        {run !== null && isActiveRunStatus(run.status) ? (
          <div
            data-state="replay-unavailable"
            className="rounded-xl border border-sky-500/30 bg-sky-500/[0.06] px-4 py-3"
          >
            <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-sky-300">
              Run in progress
            </p>
            <p className="mt-1 text-sm leading-relaxed text-slate-300">
              Replay controls appear once the run reaches a terminal state. The detail above is
              refreshed as persisted stage, claim, rebuttal, verdict and guard events arrive; the
              timeline below shows only events the backend has already stored.
            </p>
          </div>
        ) : null}

        {replayable ? (
          <ReplayControls
            events={events.events}
            hasRebuttals={hasRebuttals}
            cursorSeq={cursorSeq}
            onCursorSeqChange={navigation.setCursor}
            timingMode={navigation.timing}
            onTimingModeChange={navigation.setTiming}
          />
        ) : null}

        <DebateTimeline
          events={events.events}
          hasRebuttals={hasRebuttals}
          cursorSeq={replayable ? cursorSeq : undefined}
        />
      </section>
    </div>
  );
}

export interface DecisionRunsPageProps {
  runId: string | null;
}

export function DecisionRunsPage({ runId }: DecisionRunsPageProps): ReactElement {
  return (
    <div className="min-h-screen bg-slate-950">
      <header className="border-b border-slate-800/80 bg-slate-900/40 px-3 py-3 sm:px-4">
        <div className="mx-auto flex max-w-[1500px] flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
              AlphaAgent · Phase 1 explainability
            </p>
            <h1 className="mt-0.5 text-lg font-semibold tracking-tight text-slate-100">
              Decision runs
            </h1>
          </div>
          <nav aria-label="Primary" className="flex flex-wrap items-center gap-2">
            <a href="#/runs" className={CONTROL_CLASS}>
              All runs
            </a>
            <a href="#/plans" className={CONTROL_CLASS}>
              Portfolio plans
            </a>
            <a href="#/" className={CONTROL_CLASS}>
              Dashboard
            </a>
          </nav>
        </div>
      </header>

      <main className="mx-auto max-w-[1500px] space-y-4 px-3 py-4 sm:px-4">
        {runId ? <DecisionRunView key={runId} runId={runId} /> : <DecisionRunList />}
      </main>
    </div>
  );
}
