import { useCallback, useId, useMemo, useState, type ReactElement, type ReactNode } from 'react';
import { cn } from '../lib/cn';
import {
  claimTypeLabel,
  claimsForSide,
  decisionSummary,
  failureHeadline,
  firstSentence,
  formatLatency,
  formatRunTimestamp,
  indexEvidence,
  isActiveRunStatus,
  isProviderFailure,
  outcomeMaturityLabel,
  partialRunReasons,
  presentText,
  rebuttalResponseLabel,
  resolveEvidenceLinks,
  runModeLabel,
  runStatusLabel,
} from '../lib/decisionRuns';
import type {
  Claim,
  DecisionDetailPayload,
  EvidenceItem,
  OutcomeMaturity,
  OutcomeView,
  Rebuttal,
  RunMode,
} from '../types';
import { Badge, type BadgeTone } from './Badge';
import { EvidenceDrawer } from './EvidenceDrawer';
import { ErrorState, SkeletonRows } from './Feedback';
import { GuardComparison } from './GuardComparison';
import { Panel } from './Panel';

/**
 * UX-01 — explain one decision.
 *
 * Reading order is deliberate: provenance header, one-sentence summary, the
 * two sides' strongest claims, targeted responses (only if the protocol had
 * them), the CIO's stated rationale, the guard comparison, then outcomes.
 *
 * Every empty state is explicit. "Pending analysis", "missing evidence",
 * "partial run", "provider failure", "heuristic fallback", "legacy data" and
 * "outcome not yet matured" are different situations, and none of them render
 * a zero or a placeholder number.
 */

const INITIAL_CLAIMS = 3;

export interface DecisionDetailScene {
  /** True when the payload is an as-of replay reduction rather than the final record. */
  active: boolean;
  atEnd: boolean;
  cursorSeq: number | null;
  /** Whether adjudication has been reached at the cursor. */
  verdictVisible: boolean;
  guardVisible?: boolean;
  /** False before the first execution event; the execution block is withheld. */
  executionVisible?: boolean;
  guardPartial?: boolean;
  outcomesVisible?: boolean;
  /** Counts withheld because they are later in the recording. */
  hiddenClaims?: number;
  hiddenEvidence?: number;
  hiddenOutcomes?: number;
  hiddenRebuttals?: number;
}

export interface DecisionDetailProps {
  detail: DecisionDetailPayload | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  /**
   * Present only while replaying. The parent already reduced `detail` to the
   * cursor; this flag controls copy that must not read "not recorded" when the
   * truth is "not reached yet", and keeps the final summary out of the scene.
   */
  scene?: DecisionDetailScene;
  /**
   * Offline viewer sets this to false so an http(s) source URI is displayed but
   * never clickable — the page must be able to make zero external calls.
   */
  allowExternalLinks?: boolean;
  className?: string;
}

type Side = 'bull' | 'bear';
type NoticeTone = 'rose' | 'amber' | 'sky' | 'slate' | 'violet';

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

const MODE_TONE: Record<RunMode, BadgeTone> = {
  live: 'emerald',
  recorded: 'sky',
  heuristic: 'amber',
  legacy: 'slate',
  synthetic: 'violet',
};

const NOTICE_CLASS: Record<NoticeTone, string> = {
  rose: 'border-rose-500/30 bg-rose-500/[0.06]',
  amber: 'border-amber-500/30 bg-amber-500/[0.06]',
  sky: 'border-sky-500/30 bg-sky-500/[0.06]',
  violet: 'border-violet-500/30 bg-violet-500/[0.06]',
  slate: 'border-slate-700/70 bg-slate-950/40',
};

const NOTICE_TITLE_CLASS: Record<NoticeTone, string> = {
  rose: 'text-rose-300',
  amber: 'text-amber-200',
  sky: 'text-sky-300',
  violet: 'text-violet-300',
  slate: 'text-slate-300',
};

const RESPONSE_TYPE_CLASS: Record<string, string> = {
  agree: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300',
  dispute: 'border-rose-500/30 bg-rose-500/10 text-rose-300',
  qualify: 'border-amber-500/30 bg-amber-500/10 text-amber-200',
  insufficient_evidence: 'border-slate-600/40 bg-slate-700/20 text-slate-300',
};

const LABEL_CLASS =
  'font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400';

const SECTION_CLASS = 'min-w-0 border-t border-slate-800/70 px-4 py-3 sm:py-4';

const FOCUS_CLASS =
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950';

interface ModeNotice {
  state: string;
  title: string;
  tone: NoticeTone;
  body: string;
}

const MODE_NOTICES: Partial<Record<RunMode, ModeNotice>> = {
  heuristic: {
    state: 'heuristic-fallback',
    title: 'Heuristic fallback',
    tone: 'amber',
    body: 'This decision was produced by the deterministic heuristic, not by a live bull/bear debate. Missing claims or responses are absent because no model produced them.',
  },
  legacy: {
    state: 'legacy-data',
    title: 'Legacy report',
    tone: 'slate',
    body: 'Recorded before the claim, evidence and targeted-response protocol. Claims and responses are absent because they were never produced, not because they were empty.',
  },
  synthetic: {
    state: 'synthetic-run',
    title: 'Synthetic run',
    tone: 'violet',
    body: 'Fixture data used to exercise the interface. It is not a real market decision and no outcome should be inferred from it.',
  },
};

function badgeToneForStatus(status: string | null | undefined): BadgeTone {
  return STATUS_TONE[(status ?? '').trim().toLowerCase()] ?? 'slate';
}

function badgeToneForMode(mode: string | null | undefined): BadgeTone {
  const key = (mode ?? '').trim().toLowerCase();
  return (MODE_TONE as Record<string, BadgeTone | undefined>)[key] ?? 'slate';
}

function sideLabel(side: Side): string {
  return side === 'bull' ? 'Bull' : 'Bear';
}

function StateNotice({
  state,
  title,
  tone = 'slate',
  children,
}: {
  state: string;
  title: string;
  tone?: NoticeTone;
  children: ReactNode;
}): ReactElement {
  return (
    <div data-state={state} className={cn('min-w-0 rounded-lg border px-3 py-2.5', NOTICE_CLASS[tone])}>
      <p className={cn('font-mono text-[10px] font-semibold uppercase tracking-[0.16em]', NOTICE_TITLE_CLASS[tone])}>
        {title}
      </p>
      <div className="mt-1 text-sm leading-relaxed text-slate-300">{children}</div>
    </div>
  );
}

function InlineEmpty({ state, children }: { state?: string; children: ReactNode }): ReactElement {
  return (
    <div
      data-state={state}
      className="rounded-lg border border-dashed border-slate-800 bg-slate-950/40 px-3 py-3 text-sm leading-relaxed text-slate-400"
    >
      {children}
    </div>
  );
}

function SectionHeading({
  id,
  title,
  hint,
  aside,
}: {
  id: string;
  title: string;
  hint?: string;
  aside?: ReactNode;
}): ReactElement {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
      <div className="min-w-0">
        <h3 id={id} className={cn(LABEL_CLASS, 'text-slate-400')}>
          {title}
        </h3>
        {hint ? <p className="mt-0.5 max-w-3xl text-sm text-slate-400">{hint}</p> : null}
      </div>
      {aside ? <div className="min-w-0">{aside}</div> : null}
    </div>
  );
}

/** A button that opens the already-loaded evidence drawer at the first link. */
function EvidenceLinkButton({
  ids,
  evidenceIndex,
  onOpenEvidence,
  highlighted = false,
  className,
}: {
  ids: readonly string[] | null | undefined;
  evidenceIndex: ReadonlyMap<string, EvidenceItem>;
  onOpenEvidence: (evidenceId: string | null) => void;
  /** True when this evidence belongs to the selected response's target claim. */
  highlighted?: boolean;
  className?: string;
}): ReactElement {
  const { items, missingIds } = resolveEvidenceLinks(ids, evidenceIndex);
  const total = items.length + missingIds.length;

  if (total === 0) {
    return <span className={cn('text-sm text-slate-400', className)}>No evidence linked</span>;
  }

  if (items.length === 0) {
    return (
      <span data-state="missing-evidence" className={cn('text-sm text-amber-300', className)}>
        {total} evidence link{total === 1 ? '' : 's'} · {missingIds.length} missing from this record
      </span>
    );
  }

  const label = `Evidence: ${items.length} stored${missingIds.length > 0 ? ` · ${missingIds.length} missing` : ''}`;
  return (
    <button
      type="button"
      onClick={() => onOpenEvidence(items[0]?.id ?? null)}
      aria-label={`Open ${label.toLowerCase()}`}
      data-highlighted={highlighted ? 'true' : undefined}
      className={cn(
        'inline-flex items-center rounded border px-2 py-1 font-mono text-[11px] font-semibold uppercase tracking-wider transition-colors',
        highlighted
          ? 'border-cyan-300/70 bg-cyan-400/15 text-cyan-100 ring-2 ring-cyan-400/40'
          : 'border-cyan-500/30 bg-cyan-500/[0.07] text-cyan-200 hover:bg-cyan-500/15',
        FOCUS_CLASS,
        className,
      )}
    >
      {label}
    </button>
  );
}

function ClaimCard({
  claim,
  side,
  evidenceIndex,
  onOpenEvidence,
  highlighted = false,
  highlightedEvidenceIds,
}: {
  claim: Claim;
  side: Side;
  evidenceIndex: ReadonlyMap<string, EvidenceItem>;
  onOpenEvidence: (evidenceId: string | null) => void;
  highlighted?: boolean;
  highlightedEvidenceIds?: ReadonlySet<string>;
}): ReactElement {
  const statement = presentText(claim?.statement) ?? 'No statement was recorded for this claim.';
  const stance = presentText(claim?.stance);
  const limitation = presentText(claim?.limitations);
  const horizon = presentText(claim?.horizon);
  const claimId = presentText(claim?.claim_id);
  const claimEvidenceIds = Array.isArray(claim?.evidence_ids) ? claim.evidence_ids : [];
  const evidenceHighlighted =
    highlighted ||
    claimEvidenceIds.some(
      (id) => typeof id === 'string' && (highlightedEvidenceIds?.has(id) ?? false),
    );

  return (
    <article
      aria-label={`${sideLabel(side)} claim${claimId ? ` ${claimId}` : ''}`}
      data-highlighted={highlighted ? 'true' : undefined}
      className={cn(
        'min-w-0 rounded-lg border p-3',
        side === 'bull'
          ? 'border-emerald-500/30 bg-emerald-500/[0.05]'
          : 'border-rose-500/30 bg-rose-500/[0.05]',
        highlighted && 'ring-2 ring-cyan-400/60 ring-offset-2 ring-offset-slate-950',
      )}
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span
          className={cn(
            'font-mono text-[10px] font-semibold uppercase tracking-[0.14em]',
            side === 'bull' ? 'text-emerald-300' : 'text-rose-300',
          )}
        >
          {sideLabel(side)}
        </span>
        <span className="rounded border border-slate-700 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-slate-300">
          {claimTypeLabel(claim?.claim_type)}
        </span>
        {highlighted ? (
          <span className="rounded border border-cyan-500/40 bg-cyan-500/10 px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider text-cyan-200">
            Target of selected response
          </span>
        ) : null}
        {claimId ? (
          <span className="ml-auto break-all font-mono text-[10px] text-slate-400">{claimId}</span>
        ) : null}
      </div>

      <p className="mt-1 break-words text-sm leading-relaxed text-slate-400">
        <span className="text-slate-400">Stance: </span>
        {stance ?? 'Not stated'}
      </p>

      <p className="mt-1.5 whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-100">
        {statement}
      </p>

      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <span className={horizon ? 'text-sm text-slate-400' : 'text-sm text-slate-400'}>
          {horizon ? `Horizon: ${horizon}` : 'Horizon not stated'}
        </span>
        <EvidenceLinkButton
          ids={claimEvidenceIds}
          evidenceIndex={evidenceIndex}
          onOpenEvidence={onOpenEvidence}
          highlighted={evidenceHighlighted}
        />
      </div>

      {limitation ? (
        <p className="mt-1.5 break-words text-sm leading-relaxed text-amber-200/90">
          Limitation: {limitation}
        </p>
      ) : null}
    </article>
  );
}

function ClaimColumn({
  side,
  claims,
  mode,
  status,
  evidenceIndex,
  onOpenEvidence,
  replayActive = false,
  hiddenCount = 0,
  highlightedClaimId = null,
  highlightedEvidenceIds,
}: {
  side: Side;
  claims: Claim[];
  mode: string;
  status: string;
  evidenceIndex: ReadonlyMap<string, EvidenceItem>;
  onOpenEvidence: (evidenceId: string | null) => void;
  replayActive?: boolean;
  hiddenCount?: number;
  highlightedClaimId?: string | null;
  highlightedEvidenceIds?: ReadonlySet<string>;
}): ReactElement {
  const [expanded, setExpanded] = useState(false);
  const listId = useId();
  const highlightedIndex = highlightedClaimId
    ? claims.findIndex((claim) => presentText(claim?.claim_id) === highlightedClaimId)
    : -1;
  // A selected response can target a claim beyond the first three. Showing only
  // the default slice would highlight nothing, so the whole side is revealed in
  // that case rather than scrolling to a claim that is not rendered.
  const forceExpanded = highlightedIndex >= INITIAL_CLAIMS;
  const visible = expanded || forceExpanded ? claims : claims.slice(0, INITIAL_CLAIMS);
  const hasMore = claims.length > INITIAL_CLAIMS;
  const name = sideLabel(side);

  let emptyCopy = `No ${name.toLowerCase()} claims were recorded for this run.`;
  if (mode === 'legacy') {
    emptyCopy = 'Legacy report — individual claims were not recorded in this format.';
  } else if (mode === 'heuristic') {
    emptyCopy = `Heuristic fallback — no ${name.toLowerCase()} debate claim was produced.`;
  } else if (replayActive && hiddenCount > 0) {
    emptyCopy = `No ${name.toLowerCase()} claims have been reached at this replay cursor.`;
  } else if (replayActive) {
    emptyCopy = `No ${name.toLowerCase()} claims have been recorded yet at this replay cursor.`;
  } else if (isActiveRunStatus(status)) {
    emptyCopy = `No ${name.toLowerCase()} claims yet — analysis is still in progress.`;
  }

  return (
    <section aria-label={`${name} claims`} className="min-w-0">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3
          className={cn(
            'font-mono text-[11px] font-semibold uppercase tracking-[0.16em]',
            side === 'bull' ? 'text-emerald-300' : 'text-rose-300',
          )}
        >
          {name} case
        </h3>
        <span className="font-mono text-[10px] uppercase tracking-wider text-slate-400">
          {claims.length} claim{claims.length === 1 ? '' : 's'}
        </span>
      </div>

      {claims.length === 0 ? (
        <InlineEmpty state={`no-${side}-claims`}>{emptyCopy}</InlineEmpty>
      ) : (
        <>
          <ul id={listId} className="mt-2 space-y-2">
            {visible.map((claim, index) => (
              <li key={presentText(claim?.claim_id) ?? `${side}-claim-${index}`} className="min-w-0">
                <ClaimCard
                  claim={claim}
                  side={side}
                  evidenceIndex={evidenceIndex}
                  onOpenEvidence={onOpenEvidence}
                  highlighted={presentText(claim?.claim_id) === highlightedClaimId}
                  highlightedEvidenceIds={highlightedEvidenceIds}
                />
              </li>
            ))}
          </ul>

          {hasMore ? (
            <button
              type="button"
              onClick={() => setExpanded((value) => !value)}
              aria-expanded={expanded}
              aria-controls={listId}
              className={cn(
                'mt-2 rounded border border-slate-700 bg-slate-950/60 px-2.5 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-wider text-slate-300 transition-colors hover:bg-slate-800',
                FOCUS_CLASS,
              )}
            >
              {expanded
                ? `Show fewer ${side} claims`
                : `Show all ${claims.length} ${side} claims`}
            </button>
          ) : null}
        </>
      )}
    </section>
  );
}

function TargetedResponse({
  rebuttal,
  target,
  evidenceIndex,
  onOpenEvidence,
  selected = false,
  onSelect,
  highlightedEvidenceIds,
}: {
  rebuttal: Rebuttal;
  target: Claim | undefined;
  evidenceIndex: ReadonlyMap<string, EvidenceItem>;
  onOpenEvidence: (evidenceId: string | null) => void;
  selected?: boolean;
  onSelect?: () => void;
  highlightedEvidenceIds?: ReadonlySet<string>;
}): ReactElement {
  const responder = (rebuttal?.responding_agent ?? '').trim().toLowerCase();
  const responderName = responder === 'bear' ? 'Bear' : 'Bull';
  const responseType = (rebuttal?.response_type ?? '').trim().toLowerCase();
  const explanation = presentText(rebuttal?.explanation);
  const positionChange = presentText(rebuttal?.position_change);
  const targetId = presentText(rebuttal?.target_claim_id);
  const rebuttalId = presentText(rebuttal?.rebuttal_id);
  const evidenceIds = Array.isArray(rebuttal?.evidence_ids) ? rebuttal.evidence_ids : [];
  const rebuttalEvidenceHighlighted =
    evidenceIds.some((id) => typeof id === 'string' && (highlightedEvidenceIds?.has(id) ?? false));

  return (
    <article
      aria-label={`${responderName} response to claim ${targetId ?? 'unknown'}`}
      data-selected={selected ? 'true' : undefined}
      className={cn(
        'min-w-0 rounded-lg border bg-slate-950/40 p-3',
        selected
          ? 'border-cyan-400/70 bg-cyan-500/[0.06] ring-2 ring-cyan-400/30'
          : 'border-slate-800/70',
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={cn(
            'font-mono text-[10px] font-semibold uppercase tracking-[0.14em]',
            responder === 'bear' ? 'text-rose-300' : 'text-emerald-300',
          )}
        >
          {responderName} responds
        </span>
        <span
          className={cn(
            'rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider',
            RESPONSE_TYPE_CLASS[responseType] ??
              'border-slate-600/40 bg-slate-700/20 text-slate-300',
          )}
        >
          {rebuttalResponseLabel(responseType)}
        </span>
        {onSelect ? (
          <button
            type="button"
            onClick={onSelect}
            aria-pressed={selected}
            aria-label={`Highlight target claim for response ${rebuttalId ?? targetId ?? 'unknown'}`}
            className={cn(
              'rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider transition-colors',
              selected
                ? 'border-cyan-400/70 bg-cyan-400/15 text-cyan-100'
                : 'border-slate-700 bg-slate-950/60 text-slate-300 hover:bg-slate-800',
              FOCUS_CLASS,
            )}
          >
            {selected ? 'Target shown' : 'Show target'}
          </button>
        ) : null}
        {rebuttalId ? (
          <span className="ml-auto break-all font-mono text-[10px] text-slate-400">
            {rebuttalId}
          </span>
        ) : null}
      </div>

      <p className="mt-1 break-words text-sm text-slate-400">
        <span className="text-slate-400">Target: </span>
        {target ? (
          <>
            {sideLabel((target.agent_role ?? '').toLowerCase() === 'bear' ? 'bear' : 'bull')} claim —{' '}
            {firstSentence(target.statement, 160) ?? target.claim_id}
          </>
        ) : (
          <span className="text-amber-300">
            Claim {targetId ?? 'unknown'} — not present in this record.
          </span>
        )}
      </p>

      <p className="mt-1.5 whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-200">
        {explanation ?? 'No explanation was recorded for this response.'}
      </p>

      {positionChange ? (
        <p className="mt-1.5 break-words text-sm leading-relaxed text-slate-300">
          <span className="text-slate-400">Position change: </span>
          {positionChange}
        </p>
      ) : null}

      <div className="mt-2">
        <EvidenceLinkButton
          ids={evidenceIds}
          evidenceIndex={evidenceIndex}
          onOpenEvidence={onOpenEvidence}
          highlighted={selected || rebuttalEvidenceHighlighted}
        />
      </div>
    </article>
  );
}

function outcomeValue(value: string | null | undefined): string {
  return presentText(value) ?? 'Not available';
}

function OutcomeCard({
  outcome,
  replay = false,
}: {
  outcome: OutcomeView;
  replay?: boolean;
}): ReactElement {
  const maturity = (outcome?.maturity ?? '').trim().toLowerCase() as OutcomeMaturity | '';
  const horizonDays =
    typeof outcome?.horizon_days === 'number' && Number.isFinite(outcome.horizon_days)
      ? outcome.horizon_days
      : null;
  const horizonLabel = horizonDays === null ? 'Horizon unknown' : `${horizonDays}-day horizon`;
  const tone: BadgeTone =
    maturity === 'evaluated'
      ? 'emerald'
      : maturity === 'pending'
        ? 'amber'
        : maturity === 'matured'
          ? 'sky'
          : 'slate';

  let explanation: string;
  if (maturity === 'pending') {
    explanation = 'Not yet matured — no measurement exists at this horizon yet.';
  } else if (maturity === 'matured') {
    explanation = 'The horizon has elapsed, but evaluation has not run yet.';
  } else if (maturity === 'unavailable') {
    explanation = 'Outcome unavailable for this run.';
  } else {
    explanation = 'Outcome state is not recognised by this version.';
  }
  const recordedReason = presentText(outcome?.unavailable_reason);
  const basis = outcome?.measurement_basis ?? null;
  const basisLabel = presentText(basis?.basis);
  const referenceAt = presentText(basis?.reference_at);
  const entryPrice = presentText(outcome?.price_at_decision);
  const horizonPrice = presentText(outcome?.price_at_horizon);
  const observedFrom = presentText(outcome?.price_observed_at_decision);
  const observedTo = presentText(outcome?.price_observed_at);
  const decisionReturn = presentText(outcome?.decision_return_pct);

  return (
    <article
      aria-label={`${horizonLabel} outcome`}
      data-state={maturity === 'pending' ? 'outcome-pending' : undefined}
      className="min-w-0 rounded-lg border border-slate-800/70 bg-slate-950/40 px-3 py-2.5"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="font-mono text-xs font-semibold uppercase tracking-wider text-slate-300">
          {horizonLabel}
        </h4>
        <Badge tone={tone}>{outcomeMaturityLabel(maturity || 'unavailable')}</Badge>
      </div>

      {maturity === 'evaluated' ? (
        <dl className="mt-2 space-y-1 text-sm">
          <div className="flex items-baseline justify-between gap-2">
            <dt className="text-slate-400">Signal score</dt>
            <dd className="text-right text-slate-200">{outcomeValue(outcome?.signal_score)}</dd>
          </div>
          <div className="flex items-baseline justify-between gap-2">
            <dt className="text-slate-400">Benchmark</dt>
            <dd className="text-right text-slate-200">{outcomeValue(outcome?.benchmark)}</dd>
          </div>
          <div className="flex items-baseline justify-between gap-2">
            <dt className="text-slate-400">Portfolio impact</dt>
            <dd className="text-right text-slate-200">{outcomeValue(outcome?.portfolio_impact)}</dd>
          </div>
          {replay ? (
            <>
              <div className="flex items-baseline justify-between gap-2">
                <dt className="text-slate-400">Decision return</dt>
                <dd className="text-right text-slate-200">{outcomeValue(decisionReturn)}</dd>
              </div>
              <div className="flex items-baseline justify-between gap-2">
                <dt className="text-slate-400">Entry → horizon</dt>
                <dd className="text-right font-mono text-xs text-slate-200">
                  {outcomeValue(entryPrice)} → {outcomeValue(horizonPrice)}
                </dd>
              </div>
              <div className="flex items-baseline justify-between gap-2">
                <dt className="text-slate-400">Observed</dt>
                <dd className="text-right text-xs text-slate-300">
                  {observedFrom ? formatRunTimestamp(observedFrom) : 'Unknown'}
                  {' → '}
                  {observedTo ? formatRunTimestamp(observedTo) : 'Unknown'}
                </dd>
              </div>
              <div className="flex items-baseline justify-between gap-2">
                <dt className="text-slate-400">Basis</dt>
                <dd className="text-right text-xs text-slate-300">
                  {basisLabel ?? 'Not recorded'}
                  {referenceAt ? ` from ${formatRunTimestamp(referenceAt)}` : ''}
                </dd>
              </div>
              <div className="flex items-baseline justify-between gap-2">
                <dt className="text-slate-400">Revision</dt>
                <dd className="text-right font-mono text-xs text-slate-200">
                  {outcome?.revision ?? 'Not recorded'}
                </dd>
              </div>
            </>
          ) : null}
        </dl>
      ) : (
        <p className="mt-2 text-sm leading-relaxed text-slate-400">{explanation}</p>
      )}

      {recordedReason && maturity !== 'evaluated' ? (
        <p className="mt-2 break-words text-xs leading-relaxed text-slate-400">
          {recordedReason}
        </p>
      ) : null}

      <p className="mt-2 font-mono text-[10px] uppercase tracking-wider text-slate-400">
        Method {presentText(outcome?.method_version) ?? 'not recorded'}
      </p>
    </article>
  );
}

export function DecisionDetail({
  detail,
  loading,
  error,
  onRetry,
  scene,
  allowExternalLinks = true,
  className,
}: DecisionDetailProps): ReactElement {
  const headingId = useId();
  const summaryId = useId();
  const claimsId = useId();
  const responsesId = useId();
  const cioId = useId();
  const evidenceId = useId();
  const guardId = useId();
  const outcomesId = useId();

  const [drawer, setDrawer] = useState<{ open: boolean; initialEvidenceId: string | null }>({
    open: false,
    initialEvidenceId: null,
  });
  const [selectedRebuttalId, setSelectedRebuttalId] = useState<string | null>(null);

  const openEvidence = useCallback((evidenceIdToOpen: string | null) => {
    setDrawer({ open: true, initialEvidenceId: evidenceIdToOpen });
  }, []);
  const closeEvidence = useCallback(() => {
    setDrawer({ open: false, initialEvidenceId: null });
  }, []);

  const evidence = useMemo(
    () => (Array.isArray(detail?.evidence) ? detail.evidence.filter(Boolean) : []),
    [detail],
  );
  const evidenceIndex = useMemo(() => indexEvidence(evidence), [evidence]);
  const claims = useMemo(
    () => (Array.isArray(detail?.claims) ? detail.claims.filter(Boolean) : []),
    [detail],
  );
  const claimsById = useMemo(() => {
    const map = new Map<string, Claim>();
    for (const claim of claims) {
      const id = presentText(claim?.claim_id);
      if (id) map.set(id, claim);
    }
    return map;
  }, [claims]);

  if (!detail) {
    return (
      <Panel
        title="Decision detail"
        subtitle={error ? 'The selected run could not be loaded.' : 'Select a run to inspect.'}
        bodyClassName="p-3 sm:p-4"
        className={className}
      >
        {error ? (
          <ErrorState message={error} onRetry={onRetry} />
        ) : loading ? (
          <SkeletonRows rows={6} />
        ) : (
          <InlineEmpty>
            No decision run selected. Choose one from the run list to see its claims, evidence and
            outcomes.
          </InlineEmpty>
        )}
      </Panel>
    );
  }

  const run = detail.run;
  const mode = (run?.mode ?? '').trim().toLowerCase();
  const status = (run?.status ?? '').trim().toLowerCase();
  const modeLabel = runModeLabel(mode);
  const statusLabel = runStatusLabel(status);
  const ticker = presentText(run?.ticker)?.toUpperCase() ?? 'Unknown ticker';
  const startedLabel = formatRunTimestamp(run?.started_at);
  const completedLabel = formatRunTimestamp(run?.completed_at);
  const latencyLabel = formatLatency(run?.latency_ms);
  const modeNotice = (MODE_NOTICES as Partial<Record<string, ModeNotice>>)[mode];

  const summary = decisionSummary(detail);
  const providerFailure = status === 'failed' && isProviderFailure(run?.error_code);
  const partialReasons = partialRunReasons(detail);
  const hasMissingEvidence = claims.length > 0 && evidence.length === 0;

  const replayScene = scene?.active === true;
  const verdictVisible = !replayScene || scene?.verdictVisible !== false;
  const guardVisible = !replayScene || scene?.guardVisible !== false;
  const executionVisible = !replayScene || scene?.executionVisible !== false;
  const outcomesVisible = !replayScene || scene?.outcomesVisible !== false;
  const outcomeSchedule = detail.outcome_schedule ?? null;
  // An as-of status of "collecting" on a completed replay is a position in the
  // recording, not a live run. The live-progress notice belongs only outside the
  // replay scene; the replay-scene notice explains the cursor.
  const active = isActiveRunStatus(status) && !replayScene;
  const sceneSummary =
    replayScene && !verdictVisible
      ? 'Replay scene — adjudication has not been reached at this cursor. The final summary is pinned outside the scene and is not shown here.'
      : summary;

  const rebuttals = Array.isArray(detail.rebuttals) ? detail.rebuttals.filter(Boolean) : [];
  const outcomes = Array.isArray(detail.outcomes) ? detail.outcomes.filter(Boolean) : [];
  const decisiveEntries = (Array.isArray(detail.decisive_claim_ids) ? detail.decisive_claim_ids : [])
    .filter((id): id is string => typeof id === 'string' && id.trim() !== '')
    .map((id) => ({ id, claim: claimsById.get(id) }));
  const strongestObjection = presentText(detail.strongest_objection);
  const rationale = presentText(detail.rationale);
  const action = presentText(detail.action);

  const selectedRebuttal =
    selectedRebuttalId === null
      ? null
      : (rebuttals.find((rebuttal) => presentText(rebuttal?.rebuttal_id) === selectedRebuttalId) ??
        null);
  const highlightedClaimId = presentText(selectedRebuttal?.target_claim_id);
  const targetClaim = highlightedClaimId ? claimsById.get(highlightedClaimId) : undefined;
  const highlightedEvidenceIds = new Set<string>();
  for (const id of selectedRebuttal?.evidence_ids ?? []) {
    if (typeof id === 'string' && id.trim() !== '') highlightedEvidenceIds.add(id.trim());
  }
  for (const id of targetClaim?.evidence_ids ?? []) {
    if (typeof id === 'string' && id.trim() !== '') highlightedEvidenceIds.add(id.trim());
  }

  return (
    <article
      aria-labelledby={headingId}
      className={cn(
        'min-w-0 overflow-hidden rounded-xl border border-slate-800/80 bg-slate-900/50 shadow-panel',
        className,
      )}
    >
      <header className="border-b border-slate-800/70 px-4 py-3 sm:py-4">
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
          <div className="min-w-0">
            <p className={LABEL_CLASS}>Decision run</p>
            <h2
              id={headingId}
              className="mt-0.5 break-words font-mono text-xl font-semibold tracking-tight text-slate-100"
            >
              {ticker}
            </h2>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-1.5" data-testid="run-mode-badge">
              <span className="font-mono text-[10px] uppercase tracking-wider text-slate-400">
                Mode
              </span>
              <Badge tone={badgeToneForMode(mode)}>{modeLabel}</Badge>
            </span>
            <span className="inline-flex items-center gap-1.5" data-testid="run-status-badge">
              <span className="font-mono text-[10px] uppercase tracking-wider text-slate-400">
                Status
              </span>
              <Badge tone={badgeToneForStatus(status)}>{statusLabel}</Badge>
            </span>
          </div>
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-slate-400">
          {startedLabel === 'Unknown' ? (
            <span className="text-amber-300">Start time unknown</span>
          ) : (
            <span>
              Started{' '}
              <time dateTime={run?.started_at ?? undefined}>{startedLabel}</time>
            </span>
          )}
          {completedLabel !== 'Unknown' ? (
            <span>
              Completed{' '}
              <time dateTime={run?.completed_at ?? undefined}>{completedLabel}</time>
            </span>
          ) : status === 'completed' ? (
            <span className="text-amber-300">Completion time unknown</span>
          ) : null}
          <span>{latencyLabel === 'Unknown' ? 'Latency unknown' : `Latency ${latencyLabel}`}</span>
          <span>Protocol {presentText(run?.protocol_version) ?? 'version unknown'}</span>
          {presentText(run?.id) ? (
            <span className="break-all" title={run.id}>
              Run {run.id}
            </span>
          ) : null}
        </div>
      </header>

      {error ? (
        <div className="px-4 pt-3">
          <ErrorState compact message={`Could not refresh this run — ${error}`} onRetry={onRetry} />
        </div>
      ) : null}

      <section aria-labelledby={summaryId} className="border-b border-slate-800/70 px-4 py-3">
        <h3 id={summaryId} className={cn(LABEL_CLASS, 'text-slate-400')}>
          Decision summary
        </h3>
        <p
          data-testid="decision-summary"
          className="mt-1 max-w-4xl whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-100 sm:text-base"
        >
          {sceneSummary}
        </p>
        {replayScene ? (
          <p
            data-testid="replay-scene-position"
            className="mt-1 font-mono text-[10px] uppercase tracking-wider text-cyan-300"
          >
            Replay scene at sequence {scene?.cursorSeq ?? 'start'}
            {scene?.atEnd ? ' · final position' : ' · earlier content is withheld'}
          </p>
        ) : null}
      </section>

      <div className="space-y-2 border-b border-slate-800/70 px-4 py-3">
        {status === 'failed' ? (
          <StateNotice
            state={providerFailure ? 'provider-failure' : 'run-failure'}
            tone="rose"
            title={failureHeadline(run?.error_code)}
          >
            <p>
              The run did not complete. Whatever was stored before the failure is shown below;
              missing sections are labelled rather than filled in.
            </p>
            <p className="mt-1 break-all font-mono text-xs text-rose-200">
              Error code: {presentText(run?.error_code) ?? 'not recorded'}
            </p>
          </StateNotice>
        ) : null}

        {active ? (
          status === 'awaiting_approval' ? (
            <StateNotice state="awaiting-approval" tone="violet" title="Awaiting approval">
              Analysis has finished and the decision is waiting for a human decision. This screen
              records the state; it does not approve anything.
            </StateNotice>
          ) : (
            <StateNotice state="analysis-in-progress" tone="sky" title="Analysis in progress">
              The run is still at <span className="font-mono text-sky-200">{statusLabel}</span>.
              Claims, guard results and outcomes appear only once they are stored; nothing is
              generated while waiting.
            </StateNotice>
          )
        ) : null}

        {status === 'cancelled' ? (
          <StateNotice state="run-cancelled" tone="slate" title="Run cancelled">
            The run was cancelled before completion. No further stages were recorded.
          </StateNotice>
        ) : null}

        {partialReasons.length > 0 ? (
          <StateNotice state="partial-run" tone="amber" title="Partial run">
            <ul className="list-disc space-y-1 pl-4">
              {partialReasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          </StateNotice>
        ) : null}

        {hasMissingEvidence ? (
          <StateNotice state="missing-evidence" tone="amber" title="Missing evidence">
            This run recorded claims but no evidence items. Evidence references are marked as missing
            and are not reconstructed.
          </StateNotice>
        ) : null}

        {modeNotice ? (
          <StateNotice state={modeNotice.state} tone={modeNotice.tone} title={modeNotice.title}>
            {modeNotice.body}
          </StateNotice>
        ) : null}

        {replayScene && !executionVisible ? (
          <StateNotice state="execution-not-reached" tone="sky" title="Execution not reached">
            No execution event has been recorded at sequence {scene?.cursorSeq ?? 'start'}. The
            final execution state is withheld; this is not a "no trade" result.
          </StateNotice>
        ) : null}

        {!replayScene && outcomeSchedule?.pending ? (
          <StateNotice state="outcomes-pending" tone="amber" title="Outcome measurement pending">
            <p>
              A later horizon has not been measured yet. This view keeps checking for the recorded
              result; it does not infer one.
            </p>
            {outcomeSchedule.next_due_at ? (
              <p className="mt-1 font-mono text-xs text-amber-200/90">
                Next due {outcomeSchedule.next_due_at}
              </p>
            ) : null}
          </StateNotice>
        ) : null}

        {replayScene && !scene?.atEnd ? (
          <StateNotice state="replay-scene" tone="sky" title="Replay scene">
            <p>
              Only content recorded at or before sequence {scene?.cursorSeq ?? 'start'} is shown.
              Later claims, responses, the verdict, the guard and outcomes stay out of this scene.
            </p>
            <p className="mt-1 text-slate-400">
              {scene?.hiddenClaims ?? 0} claim{(scene?.hiddenClaims ?? 0) === 1 ? '' : 's'},{' '}
              {scene?.hiddenRebuttals ?? 0} response
              {(scene?.hiddenRebuttals ?? 0) === 1 ? '' : 's'},{' '}
              {scene?.hiddenEvidence ?? 0} evidence item
              {(scene?.hiddenEvidence ?? 0) === 1 ? '' : 's'} and{' '}
              {scene?.hiddenOutcomes ?? 0} outcome
              {(scene?.hiddenOutcomes ?? 0) === 1 ? '' : 's'} later in the recording.
            </p>
          </StateNotice>
        ) : null}
      </div>

      <section aria-labelledby={claimsId} className={SECTION_CLASS}>
        <SectionHeading
          id={claimsId}
          title="Claims"
          hint="The strongest three claims per side are shown by default. Expand a side to read every stored claim."
        />
        <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
          <ClaimColumn
            side="bull"
            claims={claimsForSide(claims, 'bull')}
            mode={mode}
            status={status}
            evidenceIndex={evidenceIndex}
            onOpenEvidence={openEvidence}
            replayActive={replayScene}
            highlightedClaimId={highlightedClaimId}
            highlightedEvidenceIds={highlightedEvidenceIds}
          />
          <ClaimColumn
            side="bear"
            claims={claimsForSide(claims, 'bear')}
            mode={mode}
            status={status}
            evidenceIndex={evidenceIndex}
            onOpenEvidence={openEvidence}
            replayActive={replayScene}
            highlightedClaimId={highlightedClaimId}
            highlightedEvidenceIds={highlightedEvidenceIds}
          />
        </div>
      </section>

      {detail.has_rebuttals ? (
        <section aria-labelledby={responsesId} className={SECTION_CLASS}>
          <SectionHeading
            id={responsesId}
            title="Targeted responses"
            hint="Each response names the claim it addresses and how the position changed, if at all."
            aside={
              <span className="font-mono text-[10px] uppercase tracking-wider text-slate-400">
                {rebuttals.length} response{rebuttals.length === 1 ? '' : 's'}
              </span>
            }
          />
          <div className="mt-2 space-y-2">
            {rebuttals.length === 0 ? (
              <InlineEmpty>
                This protocol includes targeted responses, but none were recorded for this run.
              </InlineEmpty>
            ) : (
              rebuttals.map((rebuttal, index) => (
                <TargetedResponse
                  key={presentText(rebuttal?.rebuttal_id) ?? `rebuttal-${index}`}
                  rebuttal={rebuttal}
                  target={
                    claimsById.get(presentText(rebuttal?.target_claim_id) ?? '') ?? undefined
                  }
                  evidenceIndex={evidenceIndex}
                  onOpenEvidence={openEvidence}
                  selected={presentText(rebuttal?.rebuttal_id) === selectedRebuttalId}
                  onSelect={() => {
                    const id = presentText(rebuttal?.rebuttal_id);
                    setSelectedRebuttalId((current) => (current === id ? null : (id ?? null)));
                  }}
                  highlightedEvidenceIds={highlightedEvidenceIds}
                />
              ))
            )}
          </div>
        </section>
      ) : null}

      <section aria-labelledby={cioId} className={SECTION_CLASS}>
        <SectionHeading id={cioId} title="CIO explanation" />

        {!verdictVisible ? (
          <div className="mt-2">
            <InlineEmpty state="verdict-not-reached">
              Replay has not reached adjudication at this cursor. The verdict, decisive claims,
              strongest objection and rationale stay out of the scene until the decision is
              recorded.
            </InlineEmpty>
          </div>
        ) : (
          <>
        <dl className="mt-2 grid grid-cols-1 gap-3 md:grid-cols-3">
          <div className="min-w-0">
            <dt className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
              Action
            </dt>
            <dd className="mt-0.5 text-sm leading-relaxed text-slate-300">
              {action ?? 'Not available — this run record does not store a separate action field.'}
            </dd>
          </div>

          <div className="min-w-0">
            <dt className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
              Decisive evidence
            </dt>
            <dd className="mt-0.5 text-sm leading-relaxed text-slate-300">
              {decisiveEntries.length === 0 ? (
                <span className="text-slate-400">No decisive claims were recorded.</span>
              ) : (
                <ul className="space-y-2">
                  {decisiveEntries.map(({ id, claim }) => (
                    <li key={id} className="min-w-0">
                      {claim ? (
                        <>
                          <span
                            className={cn(
                              'font-mono text-[10px] font-semibold uppercase tracking-wider',
                              (claim.agent_role ?? '').toLowerCase() === 'bear'
                                ? 'text-rose-300'
                                : 'text-emerald-300',
                            )}
                          >
                            {(claim.agent_role ?? '').toLowerCase() === 'bear' ? 'Bear' : 'Bull'}
                          </span>
                          <p className="whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-200">
                            {presentText(claim.statement) ?? 'No statement was recorded.'}
                          </p>
                          <EvidenceLinkButton
                            ids={claim.evidence_ids}
                            evidenceIndex={evidenceIndex}
                            onOpenEvidence={openEvidence}
                            className="mt-1"
                          />
                        </>
                      ) : (
                        <span className="text-amber-300">
                          Claim {id} — not present in this record.
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </dd>
          </div>

          <div className="min-w-0">
            <dt className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
              Strongest remaining objection
            </dt>
            <dd className="mt-0.5 whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-300">
              {strongestObjection ?? (
                <span className="text-slate-400">No strongest objection was recorded.</span>
              )}
            </dd>
          </div>
        </dl>

        <div className="mt-3 min-w-0">
          <h4 className={LABEL_CLASS}>Rationale</h4>
          <div
            role="region"
            aria-label="Full CIO rationale"
            tabIndex={0}
            className={cn(
              'mt-1 max-h-72 overflow-y-auto rounded-md border border-slate-800/60 bg-slate-950/40 px-3 py-2',
              FOCUS_CLASS,
            )}
          >
            <p className="whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-200">
              {rationale ?? 'No CIO rationale was recorded for this run.'}
            </p>
          </div>
        </div>
          </>
        )}
      </section>

      <section aria-labelledby={evidenceId} className={SECTION_CLASS}>
        <SectionHeading
          id={evidenceId}
          title="Stored evidence"
          hint="Open any item to read its excerpt, typed observation, timestamps and data-quality flags."
          aside={
            <span className="font-mono text-[10px] uppercase tracking-wider text-slate-400">
              {evidence.length} item{evidence.length === 1 ? '' : 's'}
            </span>
          }
        />
        {evidence.length === 0 ? (
          <div className="mt-2">
            <InlineEmpty state={replayScene ? 'evidence-not-reached' : 'missing-evidence'}>
              {replayScene
                ? `No evidence item has been reached at this replay cursor.${
                    (scene?.hiddenEvidence ?? 0) > 0
                      ? ` ${scene?.hiddenEvidence} more item${(scene?.hiddenEvidence ?? 0) === 1 ? '' : 's'} appear later in the recording.`
                      : ''
                  }`
                : 'No evidence was stored for this run. Claims that reference evidence show it as missing rather than reconstructed.'}
            </InlineEmpty>
          </div>
        ) : (
          <ul className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
            {evidence.map((item, index) => {
              const publisher = presentText(item?.publisher);
              const sourceType = presentText(item?.source_type);
              const publication = formatRunTimestamp(item?.published_at);
              const itemId = presentText(item?.id);
              const isHighlighted = itemId !== null && highlightedEvidenceIds.has(itemId);
              return (
                <li key={itemId ?? `evidence-${index}`} className="min-w-0">
                  <button
                    type="button"
                    onClick={() => openEvidence(item?.id ?? null)}
                    aria-label={`Open evidence from ${publisher ?? sourceType ?? item?.id ?? 'unknown source'}`}
                    data-highlighted={isHighlighted ? 'true' : undefined}
                    className={cn(
                      'w-full min-w-0 rounded-lg border px-3 py-2.5 text-left transition-colors',
                      isHighlighted
                        ? 'border-cyan-400/70 bg-cyan-500/[0.08] ring-2 ring-cyan-400/30'
                        : 'border-slate-800/70 bg-slate-950/40 hover:border-slate-700 hover:bg-slate-900/40',
                      FOCUS_CLASS,
                    )}
                  >
                    <span className="block break-words text-sm font-medium text-slate-200">
                      {publisher ?? 'Publisher unknown'}
                    </span>
                    <span className="mt-0.5 block break-words text-sm text-slate-400">
                      {sourceType ?? 'Source type unknown'}
                    </span>
                    <span className="mt-1 block font-mono text-[10px] uppercase tracking-wider text-slate-400">
                      Retrieved {formatRunTimestamp(item?.retrieved_at)}
                    </span>
                    <span
                      className={cn(
                        'mt-0.5 block font-mono text-[10px] uppercase tracking-wider',
                        publication === 'Unknown' ? 'text-amber-300' : 'text-slate-400',
                      )}
                    >
                      {publication === 'Unknown'
                        ? 'Publication time unknown'
                        : `Published ${publication}`}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section aria-labelledby={guardId} className={SECTION_CLASS}>
        <SectionHeading
          id={guardId}
          title="Guard comparison"
          hint="The deterministic risk guard compares the requested order with what it allowed, check by check."
        />
        <div className="mt-2">
          {!guardVisible ? (
            <InlineEmpty state="guard-not-reached">
              Replay has not reached validation at this cursor. The guard comparison stays out of
              the scene until it is recorded.
            </InlineEmpty>
          ) : (
            <GuardComparison guard={detail.guard} />
          )}
        </div>
      </section>

      <section aria-labelledby={outcomesId} className={cn(SECTION_CLASS, 'border-b-0')}>
        <SectionHeading
          id={outcomesId}
          title="Outcomes"
          hint="Only horizons the run actually recorded appear here. A null value is not a zero."
        />
        {!outcomesVisible ? (
          <div className="mt-2">
            <InlineEmpty state="outcomes-not-reached">
              Replay has not reached outcome measurement at this cursor. No performance is implied.
            </InlineEmpty>
          </div>
        ) : outcomes.length === 0 ? (
          <div className="mt-2">
            <InlineEmpty state="no-outcomes">
              {replayScene
                ? `No outcome horizon has been reached at this replay cursor.${
                    (scene?.hiddenOutcomes ?? 0) > 0
                      ? ` ${scene?.hiddenOutcomes} more appear later in the recording.`
                      : ''
                  }`
                : 'No outcome horizons were recorded for this run. No performance is implied.'}
            </InlineEmpty>
          </div>
        ) : (
          <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-3">
            {outcomes.map((outcome, index) => (
              <OutcomeCard
                key={`${outcome?.horizon_days ?? 'horizon'}-${index}`}
                outcome={outcome}
                replay={Boolean(scene?.active)}
              />
            ))}
          </div>
        )}
      </section>

      <EvidenceDrawer
        open={drawer.open}
        evidence={evidence}
        initialEvidenceId={drawer.initialEvidenceId}
        onClose={closeEvidence}
        allowExternalLinks={allowExternalLinks}
      />
    </article>
  );
}
