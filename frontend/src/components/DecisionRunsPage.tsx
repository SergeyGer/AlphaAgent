import { useEffect, useRef, useState, type ReactElement } from 'react';
import { cn } from '../lib/cn';
import {
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
import type { RunSummary } from '../types';
import { Badge, type BadgeTone } from './Badge';
import { DebateTimeline } from './DebateTimeline';
import { DecisionDetail } from './DecisionDetail';
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

function DecisionRunView({ runId }: { runId: string }): ReactElement {
  const { token } = useAuth();
  const detail = useDecisionRun(runId, token);
  const run = detail.data?.run ?? null;
  const active = run !== null && isActiveRunStatus(run.status);
  const events = useDecisionRunEvents(runId, token, {
    enabled: token !== null,
    pollIntervalMs: active ? 2_000 : 0,
  });
  const hasRebuttals = detail.data?.has_rebuttals ?? true;

  const [cursorSeq, setCursorSeq] = useState<number | null>(null);
  const cursorInitialised = useRef(false);
  const maxSeq = lastEventSequence(events.events);

  // Start a stored run at its tail so the full timeline is visible; the replay
  // controls then move the cursor. Once initialised, new events must not yank a
  // reader who deliberately stepped backwards.
  useEffect(() => {
    if (cursorInitialised.current || maxSeq === null) return;
    cursorInitialised.current = true;
    setCursorSeq(maxSeq);
  }, [maxSeq]);

  const replayable =
    events.events.length > 0 && run !== null && isTerminalRunStatus(run.status);

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

      <DecisionDetail
        detail={detail.data}
        loading={detail.loading}
        error={detail.error}
        onRetry={detail.refresh}
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

        {run !== null && isActiveRunStatus(run.status) ? (
          <div
            data-state="replay-unavailable"
            className="rounded-xl border border-sky-500/30 bg-sky-500/[0.06] px-4 py-3"
          >
            <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-sky-300">
              Run in progress
            </p>
            <p className="mt-1 text-sm leading-relaxed text-slate-300">
              Replay controls appear once the run reaches a terminal state. The timeline below shows
              only events the backend has already stored — no live tokens are invented.
            </p>
          </div>
        ) : null}

        {replayable ? (
          <ReplayControls
            events={events.events}
            hasRebuttals={hasRebuttals}
            cursorSeq={cursorSeq}
            onCursorSeqChange={setCursorSeq}
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
