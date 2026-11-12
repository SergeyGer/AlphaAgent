import type {
  Claim,
  ClaimType,
  DecisionDetailPayload,
  EvidenceItem,
  GuardComparison,
  OutcomeMaturity,
  RebuttalResponseType,
  RunEvent,
  RunMode,
  RunStatus,
} from '../types';

/**
 * Pure presentation helpers for the Phase 1 decision-run screens.
 *
 * Three rules drove every function here:
 *
 * 1. **Unknown is not zero.** A `null` publication time, a `null` portfolio
 *    impact and an unmatured outcome all have explicit text. None of them may
 *    fall through to a number.
 * 2. **`not_evaluated` is not `fail`.** They are separate labels and separate
 *    tones everywhere, including the guard summary counts.
 * 3. **The event stream is aggregated by stage.** Token-level events collapse
 *    into the stage they belong to; a timeline node is a pipeline step, not a
 *    token. Replay state is a pure filter over the ordered event list, so
 *    navigating forward and backward lands on exactly the same view.
 */

const UNKNOWN = 'Unknown';

/* ------------------------------------------------------------------ */
/* Mode and status labels                                              */
/* ------------------------------------------------------------------ */

export const RUN_MODE_LABELS: Record<RunMode, string> = {
  live: 'Live',
  recorded: 'Recorded',
  heuristic: 'Heuristic',
  legacy: 'Legacy',
  synthetic: 'Synthetic',
};

export const RUN_STATUS_LABELS: Record<RunStatus, string> = {
  queued: 'Queued',
  collecting: 'Collecting evidence',
  analysing: 'Analysing',
  rebutting: 'Rebutting',
  adjudicating: 'Adjudicating',
  validating: 'Validating',
  awaiting_approval: 'Awaiting approval',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

const ACTIVE_STATUSES: ReadonlySet<string> = new Set<RunStatus>([
  'queued',
  'collecting',
  'analysing',
  'rebutting',
  'adjudicating',
  'validating',
  'awaiting_approval',
]);

const TERMINAL_STATUSES: ReadonlySet<string> = new Set<RunStatus>([
  'completed',
  'failed',
  'cancelled',
]);

/** Tolerance for unknown values from an older backend. */
export function runModeLabel(mode: RunMode | string | null | undefined): string {
  const key = typeof mode === 'string' ? mode.trim().toLowerCase() : '';
  return (RUN_MODE_LABELS as Record<string, string | undefined>)[key] ?? UNKNOWN;
}

export function runStatusLabel(status: RunStatus | string | null | undefined): string {
  const key = typeof status === 'string' ? status.trim().toLowerCase() : '';
  return (RUN_STATUS_LABELS as Record<string, string | undefined>)[key] ?? UNKNOWN;
}

export function isActiveRunStatus(status: RunStatus | string | null | undefined): boolean {
  return typeof status === 'string' && ACTIVE_STATUSES.has(status.trim().toLowerCase());
}

export function isTerminalRunStatus(status: RunStatus | string | null | undefined): boolean {
  return typeof status === 'string' && TERMINAL_STATUSES.has(status.trim().toLowerCase());
}

/* ------------------------------------------------------------------ */
/* Claim / rebuttal / guard labels                                     */
/* ------------------------------------------------------------------ */

const CLAIM_TYPE_LABELS: Record<ClaimType, string> = {
  observed_fact: 'Observed fact',
  interpretation: 'Interpretation',
  forecast: 'Forecast',
};

export function claimTypeLabel(type: ClaimType | string | null | undefined): string {
  const key = typeof type === 'string' ? type.trim().toLowerCase() : '';
  return (CLAIM_TYPE_LABELS as Record<string, string | undefined>)[key] ?? 'Unclassified claim';
}

const REBUTTAL_LABELS: Record<RebuttalResponseType, string> = {
  agree: 'Agrees with claim',
  dispute: 'Disputes claim',
  qualify: 'Qualifies claim',
  insufficient_evidence: 'Insufficient evidence',
};

export function rebuttalResponseLabel(type: RebuttalResponseType | string | null | undefined): string {
  const key = typeof type === 'string' ? type.trim().toLowerCase() : '';
  return (REBUTTAL_LABELS as Record<string, string | undefined>)[key] ?? 'Unclassified response';
}

export function guardStatusLabel(status: string | null | undefined): string {
  const key = (status ?? '').trim().toLowerCase();
  if (key === 'approved') return 'Approved';
  if (key === 'clamped') return 'Clamped';
  if (key === 'rejected') return 'Rejected';
  return 'Unknown outcome';
}

/** `not_evaluated` gets its own label; it must never be rendered as `Fail`. */
export function guardCheckOutcomeLabel(outcome: string | null | undefined): string {
  const key = (outcome ?? '').trim().toLowerCase();
  if (key === 'pass') return 'Pass';
  if (key === 'fail') return 'Fail';
  if (key === 'not_evaluated') return 'Not evaluated';
  return 'Unknown outcome';
}

const MATURITY_LABELS: Record<OutcomeMaturity, string> = {
  pending: 'Not yet matured',
  matured: 'Matured — evaluation pending',
  evaluated: 'Evaluated',
  unavailable: 'Unavailable',
};

export function outcomeMaturityLabel(maturity: OutcomeMaturity | string | null | undefined): string {
  const key = typeof maturity === 'string' ? maturity.trim().toLowerCase() : '';
  return (MATURITY_LABELS as Record<string, string | undefined>)[key] ?? 'Unavailable';
}

/* ------------------------------------------------------------------ */
/* Time and latency                                                    */
/* ------------------------------------------------------------------ */

const RUN_TIMESTAMP_FORMAT = new Intl.DateTimeFormat('en-GB', {
  dateStyle: 'medium',
  timeStyle: 'medium',
  timeZone: 'UTC',
});

function parseDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/**
 * An explicit-timezone timestamp. The label is always present because a bare
 * clock time invites the reader to assume their own zone, and a second `null`
 * must never be rendered as a date-shaped placeholder.
 */
export function formatRunTimestamp(value: string | null | undefined): string {
  const date = parseDate(value);
  if (!date) return UNKNOWN;
  return `${RUN_TIMESTAMP_FORMAT.format(date)} UTC`;
}

/** `null` is "unknown", not "0 ms". */
export function formatLatency(value: number | string | null | undefined): string {
  const parsed = typeof value === 'number' ? value : Number(value);
  if (value === null || value === undefined || !Number.isFinite(parsed) || parsed < 0) {
    return 'Unknown';
  }
  if (parsed < 1_000) return `${Math.round(parsed)} ms`;
  if (parsed < 60_000) return `${(parsed / 1_000).toFixed(1)} s`;
  const minutes = Math.floor(parsed / 60_000);
  const seconds = Math.round((parsed % 60_000) / 1_000);
  return `${minutes}m ${seconds}s`;
}

/* ------------------------------------------------------------------ */
/* Summary text                                                        */
/* ------------------------------------------------------------------ */

/**
 * The one-sentence decision summary shown above everything else.
 *
 * Returns `null` for empty text; never invents a sentence.
 */
export function firstSentence(text: string | null | undefined, maxLength = 260): string | null {
  if (typeof text !== 'string') return null;
  const cleaned = text.replace(/\s+/g, ' ').trim();
  if (cleaned === '') return null;

  const match = cleaned.match(/^(.+?[.!?])(?:\s|$)/);
  const sentence = match?.[1] ?? cleaned;
  if (sentence.length <= maxLength) return sentence;

  const clipped = sentence.slice(0, maxLength - 1);
  const lastSpace = clipped.lastIndexOf(' ');
  return `${(lastSpace > 40 ? clipped.slice(0, lastSpace) : clipped).trimEnd()}…`;
}

const PROVIDER_FAILURE_PATTERN =
  /(provider|llm|upstream|openai|anthropic|gemini|ollama|rate[._-]?limit|timeout|gateway|model[._-]?(error|unavailable)|connection)/i;

/** True when an error code points at the model provider rather than the app. */
export function isProviderFailure(errorCode: string | null | undefined): boolean {
  if (typeof errorCode !== 'string' || errorCode.trim() === '') return false;
  return PROVIDER_FAILURE_PATTERN.test(errorCode);
}

export function failureHeadline(errorCode: string | null | undefined): string {
  if (isProviderFailure(errorCode)) return 'Provider failure';
  if (/budget|quota|exhaust|spend/i.test(errorCode ?? '')) {
    return 'Run halted — provider budget exhausted';
  }
  return 'Run failed';
}

/* ------------------------------------------------------------------ */
/* Evidence                                                            */
/* ------------------------------------------------------------------ */

export function indexEvidence(evidence: readonly EvidenceItem[] | null | undefined): Map<string, EvidenceItem> {
  const index = new Map<string, EvidenceItem>();
  for (const item of evidence ?? []) {
    const id = typeof item?.id === 'string' ? item.id.trim() : '';
    if (id !== '') index.set(id, item);
  }
  return index;
}

export interface EvidenceLinks {
  items: EvidenceItem[];
  missingIds: string[];
}

/** Resolve claim/rebuttal evidence ids against the stored evidence list. */
export function resolveEvidenceLinks(
  ids: readonly string[] | null | undefined,
  index: ReadonlyMap<string, EvidenceItem>,
): EvidenceLinks {
  const items: EvidenceItem[] = [];
  const missingIds: string[] = [];
  const seen = new Set<string>();

  for (const raw of ids ?? []) {
    if (typeof raw !== 'string') continue;
    const id = raw.trim();
    if (id === '' || seen.has(id)) continue;
    seen.add(id);
    const item = index.get(id);
    if (item) items.push(item);
    else missingIds.push(id);
  }

  return { items, missingIds };
}

export function isExternalHttpUrl(value: string | null | undefined): boolean {
  if (typeof value !== 'string' || value.trim() === '') return false;
  try {
    const url = new URL(value);
    return url.protocol === 'http:' || url.protocol === 'https:';
  } catch {
    return false;
  }
}

/**
 * Human-readable data-quality flags for one stored source.
 *
 * The `published_at: null` flag is deliberately explicit: "publication time
 * unknown" is a fact about the source, and a dash or epoch would name a time
 * that was never supplied.
 */
export function evidenceQualityFlags(item: EvidenceItem | null | undefined): string[] {
  if (!item) return ['No evidence item is available.'];
  const flags: string[] = [];

  if (item.published_at === null || item.published_at === undefined) {
    flags.push('Publication time unknown — the source supplied no timestamp.');
  }
  if (!item.publisher || item.publisher.trim() === '') {
    flags.push('Publisher unknown.');
  }
  if (!item.source_uri || item.source_uri.trim() === '') {
    flags.push('No source link stored.');
  } else if (!isExternalHttpUrl(item.source_uri)) {
    flags.push('Source link is not an http(s) URL and is not rendered as a link.');
  }
  if (!item.excerpt || item.excerpt.trim() === '') {
    flags.push('No excerpt stored.');
  }
  if (!item.observation) {
    flags.push('No typed observation stored.');
  }
  if (!item.content_hash || item.content_hash.trim() === '') {
    flags.push('Content hash missing.');
  }

  const published = parseDate(item.published_at);
  const retrieved = parseDate(item.retrieved_at);
  if (published && retrieved && published.getTime() > retrieved.getTime()) {
    flags.push('Publication time is later than retrieval time — check the source clock.');
  }
  if (!retrieved) {
    flags.push('Retrieval time is missing or unparseable.');
  }

  return flags;
}

/* ------------------------------------------------------------------ */
/* Timeline                                                            */
/* ------------------------------------------------------------------ */

export interface TimelineNode {
  key: string;
  stage: string;
  label: string;
  firstSeq: number;
  lastSeq: number;
  occurredAt: string;
  agents: string[];
  eventCount: number;
  failed: boolean;
  summary: string | null;
}

const STAGE_ALIASES: Record<string, string> = {
  queue: 'queued',
  started: 'queued',
  start: 'queued',
  collection: 'collecting',
  evidence: 'collecting',
  analysis: 'analysing',
  analyst: 'analysing',
  research: 'analysing',
  rebuttal: 'rebutting',
  rebuttals: 'rebutting',
  debate: 'rebutting',
  cio: 'adjudicating',
  verdict: 'adjudicating',
  adjudication: 'adjudicating',
  guard: 'validating',
  validation: 'validating',
  approval: 'awaiting_approval',
  awaiting_approval: 'awaiting_approval',
  done: 'completed',
  complete: 'completed',
  finished: 'completed',
  error: 'failed',
  failure: 'failed',
  canceled: 'cancelled',
};

function normalizeStage(stage: string): string {
  const key = stage.trim().toLowerCase().replace(/[\s-]+/g, '_');
  return STAGE_ALIASES[key] ?? key;
}

/** Infer a stage from an event type when the backend omitted `stage`. */
function inferStageFromType(type: string): string | null {
  const key = type.toLowerCase();
  if (/rebut/.test(key)) return 'rebutting';
  if (/guard|valid/.test(key)) return 'validating';
  if (/approv/.test(key)) return 'awaiting_approval';
  if (/complete|finish|done/.test(key)) return 'completed';
  if (/fail|error|cancel/.test(key)) return 'failed';
  if (/adjudic|verdict|cio/.test(key)) return 'adjudicating';
  if (/claim|analys|research/.test(key)) return 'analysing';
  if (/collect|evidence|retriev|fetch|source/.test(key)) return 'collecting';
  if (/queue|start|created/.test(key)) return 'queued';
  return null;
}

export function stageLabel(stage: string | null | undefined): string {
  if (typeof stage !== 'string' || stage.trim() === '') return 'Run';
  const key = normalizeStage(stage);
  const known = (RUN_STATUS_LABELS as Record<string, string | undefined>)[key];
  if (known) return known;
  return key
    .split('_')
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

function isRebuttalNode(stage: string, type: string): boolean {
  return /rebut/i.test(stage) || /rebuttal/i.test(type);
}

function payloadString(payload: Record<string, unknown> | null | undefined, keys: string[]): string | null {
  if (!payload || typeof payload !== 'object') return null;
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === 'string' && value.trim() !== '') {
      return firstSentence(value, 180);
    }
  }
  return null;
}

function payloadStatus(payload: Record<string, unknown> | null | undefined): string | null {
  if (!payload || typeof payload !== 'object') return null;
  const value = payload['status'];
  return typeof value === 'string' ? value.toLowerCase() : null;
}

function isFailureEvent(event: RunEvent): boolean {
  const type = (event.type ?? '').toLowerCase();
  const status = payloadStatus(event.payload);
  return (
    /(^|[._])(failed|failure|error|cancelled|canceled)$/.test(type) ||
    /provider[._-]?(fail|error|timeout)/.test(type) ||
    status === 'failed' ||
    status === 'cancelled'
  );
}

function sortEvents(events: readonly RunEvent[]): RunEvent[] {
  return [...events].sort((a, b) => {
    const aSeq = Number.isFinite(a?.seq) ? a.seq : 0;
    const bSeq = Number.isFinite(b?.seq) ? b.seq : 0;
    return aSeq - bSeq;
  });
}

function eventKey(event: RunEvent): string {
  if (typeof event?.event_id === 'string' && event.event_id.trim() !== '') return event.event_id;
  return `${event?.seq ?? 0}:${event?.type ?? 'event'}`;
}

/** Merge event pages by identity, keeping the newest copy of each event. */
export function mergeRunEvents(previous: readonly RunEvent[], incoming: readonly RunEvent[]): RunEvent[] {
  const byKey = new Map<string, RunEvent>();
  for (const event of [...previous, ...incoming]) {
    if (!event || typeof event !== 'object') continue;
    if (typeof event.seq !== 'number' || !Number.isFinite(event.seq)) continue;
    byKey.set(eventKey(event), event);
  }
  return [...byKey.values()].sort((a, b) => a.seq - b.seq);
}

/** Unique, ordered sequence numbers — the replay steps. */
export function eventSequences(events: readonly RunEvent[]): number[] {
  const sequences = new Set<number>();
  for (const event of events) {
    if (typeof event?.seq === 'number' && Number.isFinite(event.seq)) sequences.add(event.seq);
  }
  return [...sequences].sort((a, b) => a - b);
}

/**
 * Build stage-level timeline nodes.
 *
 * Events are grouped by stage (inferred from the type when `stage` is absent),
 * so a provider that emits one event per token still produces one node per
 * pipeline step. When `hasRebuttals` is false every rebuttal event is dropped:
 * the pipeline never had that stage, so showing a node would invent history.
 */
export function buildTimeline(
  events: readonly RunEvent[],
  hasRebuttals: boolean,
): TimelineNode[] {
  const nodes = new Map<string, TimelineNode>();
  const seen = new Set<string>();
  let previousStage = 'run';

  for (const event of sortEvents(events)) {
    if (!event || typeof event !== 'object') continue;
    const identity = eventKey(event);
    if (seen.has(identity)) continue;
    seen.add(identity);

    const rawStage =
      (typeof event.stage === 'string' && event.stage.trim() !== '' ? event.stage : null) ??
      inferStageFromType(event.type ?? '') ??
      previousStage;
    const stage = normalizeStage(rawStage);

    if (!hasRebuttals && isRebuttalNode(stage, event.type ?? '')) continue;
    previousStage = stage;

    const summary = payloadString(event.payload, ['summary', 'message', 'detail', 'description']);
    const agent = typeof event.agent_role === 'string' && event.agent_role.trim() !== ''
      ? event.agent_role.trim()
      : null;
    const occurredAt = event.occurred_at ?? '';

    const existing = nodes.get(stage);
    if (existing) {
      existing.lastSeq = Math.max(existing.lastSeq, event.seq);
      existing.eventCount += 1;
      existing.failed = existing.failed || isFailureEvent(event);
      if (parseDate(occurredAt) && !parseDate(existing.occurredAt)) existing.occurredAt = occurredAt;
      if (summary) existing.summary = summary;
      if (agent && !existing.agents.includes(agent)) existing.agents.push(agent);
    } else {
      nodes.set(stage, {
        key: stage,
        stage,
        label: stageLabel(stage),
        firstSeq: event.seq,
        lastSeq: event.seq,
        occurredAt,
        agents: agent ? [agent] : [],
        eventCount: 1,
        failed: isFailureEvent(event),
        summary,
      });
    }
  }

  return [...nodes.values()].sort((a, b) => a.firstSeq - b.firstSeq);
}

/**
 * Filter events for replay. `undefined` means "show everything".
 *
 * The result is sorted. A page can arrive out of order, and every presentation
 * rule below is defined on sequence, not on array position: sorting here is what
 * makes seeking backward and forward produce identical scenes from the same
 * held set.
 */
export function eventsUpTo(events: readonly RunEvent[], cursorSeq: number | null | undefined): RunEvent[] {
  if (cursorSeq === undefined) return sortEvents(events);
  const cutoff = cursorSeq === null ? -1 : cursorSeq;
  return sortEvents(events.filter((event) => typeof event?.seq === 'number' && event.seq <= cutoff));
}

/** The sequence number of the final event, or `null` when there are none. */
export function lastEventSequence(events: readonly RunEvent[]): number | null {
  const sequences = eventSequences(events);
  return sequences.length === 0 ? null : (sequences[sequences.length - 1] ?? null);
}

/**
 * The last sequence in an unbroken run starting at 1.
 *
 * A gap must not be mistaken for the tail. If events 1, 2 and 5 are held, the
 * highest sequence is 5 but the next event we can safely request is 3; asking
 * for `after_seq=5` would leave 3 and 4 missing forever. The recovery cursor is
 * therefore the last *contiguous* sequence, and 0 when even 1 is missing.
 */
export function lastContiguousSequence(events: readonly RunEvent[]): number {
  const sequences = eventSequences(events);
  let expected = 1;
  for (const seq of sequences) {
    if (seq < expected) continue;
    if (seq > expected) break;
    expected += 1;
  }
  return expected - 1;
}

/** Count of sequences at or below `upTo` that are not held locally. */
export function missingSequenceCount(events: readonly RunEvent[], upTo: number | null): number {
  if (upTo === null || upTo < 1) return 0;
  const held = new Set(eventSequences(events));
  let missing = 0;
  for (let seq = 1; seq <= upTo; seq += 1) {
    if (!held.has(seq)) missing += 1;
  }
  return missing;
}

export function isAtReplayEnd(events: readonly RunEvent[], cursorSeq: number | null): boolean {
  const last = lastEventSequence(events);
  return last === null || cursorSeq === last;
}

/**
 * Events whose persistence changes the detail payload (a stage transition, a
 * claim/rebuttal, the decision, the guard or an outcome). Token-level noise is
 * excluded so an active run's detail is not refetched on every delta.
 */
const DETAIL_REFRESH_PATTERN =
  /^(?:status\.changed|stage\.(?:started|completed|failed)|claim\.created|rebuttal\.created|decision\.proposed|guard\.evaluated|outcome\.recorded|run\.(?:created|completed)|limitation\.recorded)$/i;

export function isDetailRefreshEvent(event: RunEvent | null | undefined): boolean {
  if (!event || typeof event.type !== 'string') return false;
  const type = event.type.trim();
  if (!DETAIL_REFRESH_PATTERN.test(type)) return false;
  // A stage.started for adjudication is itself a persisted stage event; all of
  // the above are. The pattern is the allow-list, not a heuristic.
  return true;
}

/** New persisted detail-changing events after `afterSeq`, ordered by sequence. */
export function eventsRequiringDetailRefresh(
  events: readonly RunEvent[],
  afterSeq: number,
): RunEvent[] {
  return events
    .filter((event) => typeof event?.seq === 'number' && event.seq > afterSeq)
    .filter(isDetailRefreshEvent)
    .sort((left, right) => left.seq - right.seq);
}

/**
 * Replay stepping is defined on the sorted sequence list, not on array indexes:
 * the same cursor always identifies the same set of visible events, in both
 * directions.
 */
export function stepReplayCursor(
  events: readonly RunEvent[],
  cursorSeq: number | null,
  direction: 'previous' | 'next',
): number | null {
  const sequences = eventSequences(events);
  if (sequences.length === 0) return null;

  if (direction === 'next') {
    if (cursorSeq === null) return sequences[0] ?? null;
    const next = sequences.find((seq) => seq > cursorSeq);
    return next ?? sequences[sequences.length - 1] ?? null;
  }

  if (cursorSeq === null) return null;
  const previous = [...sequences].reverse().find((seq) => seq < cursorSeq);
  return previous ?? null;
}

/** Stage containing the cursor — what the screen reader announces on changes. */
export function stageAtCursor(
  events: readonly RunEvent[],
  cursorSeq: number | null,
  hasRebuttals: boolean,
): TimelineNode | null {
  const nodes = buildTimeline(eventsUpTo(events, cursorSeq), hasRebuttals);
  return nodes.length === 0 ? null : (nodes[nodes.length - 1] ?? null);
}

/* ------------------------------------------------------------------ */
/* Record completeness                                                 */
/* ------------------------------------------------------------------ */

/**
 * Reasons this stored record is visibly incomplete.
 *
 * These are observations about the record, not accusations about the decision:
 * "No CIO rationale was recorded" is verifiable from the payload, whereas
 * filling the gap with generated prose would not be.
 */
export function partialRunReasons(detail: DecisionDetailPayload | null): string[] {
  if (!detail || !detail.run) return [];
  // A failed/cancelled run has its own state notice; calling it "partial" would
  // describe a known failure as an unexplained gap.
  if (detail.run.status === 'failed' || detail.run.status === 'cancelled') return [];
  const reasons: string[] = [];
  const mode = detail.run.mode;
  const structured = mode === 'recorded' || mode === 'live' || mode === 'synthetic';

  const evidenceIndex = indexEvidence(detail.evidence);
  const referenced = new Set<string>();
  for (const claim of detail.claims ?? []) {
    for (const id of claim?.evidence_ids ?? []) {
      if (typeof id === 'string' && id.trim() !== '') referenced.add(id.trim());
    }
  }
  for (const rebuttal of detail.rebuttals ?? []) {
    for (const id of rebuttal?.evidence_ids ?? []) {
      if (typeof id === 'string' && id.trim() !== '') referenced.add(id.trim());
    }
  }

  const missing = [...referenced].filter((id) => !evidenceIndex.has(id));
  if (missing.length > 0) {
    reasons.push(
      `${missing.length} referenced evidence item${missing.length === 1 ? ' is' : 's are'} not present in this record.`,
    );
  }

  if (structured && detail.run.status === 'completed' && (detail.claims ?? []).length === 0) {
    reasons.push('No debate claims were recorded for this completed run.');
  }
  if (structured && detail.run.status === 'completed' && !detail.rationale) {
    reasons.push('No CIO rationale was recorded.');
  }
  if (!detail.guard && structured) {
    reasons.push('No guard comparison was recorded.');
  }

  return reasons;
}

/* ------------------------------------------------------------------ */
/* Convenience                                                         */
/* ------------------------------------------------------------------ */

/**
 * The single sentence shown at the top of the detail screen.
 *
 * It is always sourced from the stored rationale; when that is absent the
 * fallback states the absence rather than generating a summary.
 */
export function decisionSummary(detail: DecisionDetailPayload | null): string {
  if (!detail || !detail.run) return 'No decision run selected.';

  const fromRationale = firstSentence(detail.rationale);
  if (fromRationale) return fromRationale;

  if (detail.run.status === 'failed') {
    return `${failureHeadline(detail.run.error_code)} — no decision summary was recorded.`;
  }
  if (isActiveRunStatus(detail.run.status)) {
    return `Analysis is still in progress (${runStatusLabel(detail.run.status).toLowerCase()}); no decision summary has been recorded yet.`;
  }
  if (detail.run.status === 'cancelled') {
    return 'Run cancelled — no decision summary was recorded.';
  }
  if (detail.run.mode === 'legacy') {
    return 'Legacy decision record — no CIO rationale was recorded in a structured form.';
  }
  return 'No decision summary was recorded for this run.';
}

/** True when a claim was made by the given side. */
export function claimsForSide(claims: readonly Claim[], side: 'bull' | 'bear'): Claim[] {
  return claims.filter((claim) => (claim?.agent_role ?? '').trim().toLowerCase() === side);
}

/** Non-empty trimmed string or `null` — used to avoid rendering blank fields. */
export function presentText(value: string | null | undefined): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  return trimmed === '' ? null : trimmed;
}

/* ------------------------------------------------------------------ */
/* Run links (legacy audit rows → structured explanation)              */
/* ------------------------------------------------------------------ */

interface RunLinkCarrier {
  run_id?: unknown;
  decision_run_id?: unknown;
  run?: { id?: unknown } | null;
}

function idText(value: unknown): string | null {
  if (typeof value === 'string') return presentText(value);
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return null;
}

/**
 * The associated run id from whichever shape the API publishes.
 *
 * Phase 0 rows (`AgentDecisionLog`, executed transactions, scored outcomes) have
 * no run at all. A syntactically present but blank field is "no mapping", never
 * a link to `/runs/`.
 */
export function runLinkId(source: RunLinkCarrier | null | undefined): string | null {
  if (!source || typeof source !== 'object') return null;
  return (
    idText(source.run_id) ??
    idText(source.decision_run_id) ??
    idText(source.run?.id) ??
    null
  );
}

/** Hash route for one run, or `null` when there is no mapping. */
export function decisionRunHref(runId: string | number | null | undefined): string | null {
  const id = idText(runId);
  return id === null ? null : `#/runs/${encodeURIComponent(id)}`;
}

/* ------------------------------------------------------------------ */
/* Replay timing                                                       */
/* ------------------------------------------------------------------ */

export type ReplayTimingMode = 'compressed' | 'recorded';

export const REPLAY_TIMING_MODES: readonly ReplayTimingMode[] = ['compressed', 'recorded'];

/** Fixed delay used by compressed playback at 1×. */
export const COMPRESSED_STEP_MS = 800;

/** Floor shared by both modes so a step is never instantaneous to the eye. */
export const MIN_STEP_MS = 120;

export function isReplayTimingMode(value: unknown): value is ReplayTimingMode {
  return value === 'compressed' || value === 'recorded';
}

function eventTimeMs(event: RunEvent | null | undefined): number | null {
  if (!event) return null;
  const parsed = Date.parse(event.occurred_at);
  return Number.isNaN(parsed) ? null : parsed;
}

/**
 * Delay before the next event is revealed.
 *
 * `compressed` is a fixed 800 ms divided by speed — fast enough to review a
 * 300-event run, and explicitly *not* the recorded pace. `recorded` uses the
 * stored `occurred_at` delta between the cursor and the next sequence, divided
 * by speed, so a five-second gap plays as five seconds at 1×. When timestamps
 * are missing or non-monotonic it falls back to the compressed delay rather
 * than inventing a pace; both paths apply the accessibility floor.
 */
export function playbackDelayMs(
  events: readonly RunEvent[],
  cursorSeq: number | null,
  speed: number,
  mode: ReplayTimingMode,
): number {
  const divisor = Number.isFinite(speed) && speed > 0 ? speed : 1;
  const compressed = Math.max(MIN_STEP_MS, Math.round(COMPRESSED_STEP_MS / divisor));
  if (mode !== 'recorded') return compressed;

  const sequences = eventSequences(events);
  const nextSeq =
    cursorSeq === null ? (sequences[0] ?? null) : (sequences.find((seq) => seq > cursorSeq) ?? null);
  if (nextSeq === null) return compressed;

  const current =
    cursorSeq === null
      ? null
      : (events.find((event) => event.seq === cursorSeq) ?? null);
  const next = events.find((event) => event.seq === nextSeq) ?? null;
  const from = eventTimeMs(current);
  const to = eventTimeMs(next);
  if (current !== null && from === null) return compressed;
  if (to === null) return compressed;

  // At the start there is no previous instant. The first recorded event is
  // revealed immediately by the play handler, so this branch is only reached
  // when a caller asks for the delay without that handler.
  if (current === null) return Math.max(MIN_STEP_MS, Math.round(COMPRESSED_STEP_MS / divisor));

  const delta = to - (from as number);
  if (!Number.isFinite(delta) || delta < 0) return compressed;
  return Math.max(MIN_STEP_MS, Math.round(delta / divisor));
}

export function replayTimingLabel(mode: ReplayTimingMode, speed: number): string {
  const rate = Number.isFinite(speed) && speed > 0 ? speed : 1;
  if (mode === 'recorded') {
    return `Recorded-time playback — delays follow the stored event timestamps at ${rate}×. This is not compressed.`;
  }
  const perEvent = Math.round(COMPRESSED_STEP_MS / rate);
  return rate === 1
    ? `Compressed timing — ${COMPRESSED_STEP_MS} ms per event. This is not the recorded pace.`
    : `Compressed timing — ${perEvent} ms per event at ${rate}× (${COMPRESSED_STEP_MS} ms at 1×). This is not the recorded pace.`;
}

/* ------------------------------------------------------------------ */
/* As-of-sequence presentation                                         */
/* ------------------------------------------------------------------ */

interface SequenceIndex {
  claim: Map<string, number>;
  rebuttal: Map<string, number>;
  evidence: Map<string, number>;
  outcome: Map<string, number>;
  decision: number | null;
  guardEvents: RunEvent[];
  execution: number | null;
  terminal: number | null;
}

function payloadStringValue(
  payload: Record<string, unknown> | null | undefined,
  keys: readonly string[],
): string | null {
  if (!payload || typeof payload !== 'object') return null;
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === 'string' && value.trim() !== '') return value.trim();
    if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  }
  return null;
}

function earliest(map: Map<string, number>, key: string, seq: number): void {
  const previous = map.get(key);
  if (previous === undefined || seq < previous) map.set(key, seq);
}

const GUARD_EVENT_PATTERN = /^guard[._](evaluated|recorded)$/i;
const EXECUTION_EVENT_PATTERN = /^execution[._](recorded|rejected|failed)$/i;

function isGuardEvent(event: RunEvent): boolean {
  return GUARD_EVENT_PATTERN.test((event?.type ?? '').trim());
}

function indexEventVisibility(events: readonly RunEvent[]): SequenceIndex {
  const index: SequenceIndex = {
    claim: new Map(),
    rebuttal: new Map(),
    evidence: new Map(),
    outcome: new Map(),
    decision: null,
    guardEvents: [],
    execution: null,
    terminal: null,
  };

  for (const event of sortEvents(events)) {
    if (!event || typeof event.seq !== 'number' || !Number.isFinite(event.seq)) continue;
    const type = (event.type ?? '').trim().toLowerCase();
    const payload = event.payload ?? {};
    const seq = event.seq;

    if (/^claim\.(created|recorded)$/.test(type) || /claim.*(created|recorded)/.test(type)) {
      const id = payloadStringValue(payload, ['claim_id', 'claimId']);
      if (id) earliest(index.claim, id, seq);
    }
    if (/^rebuttal\.(created|recorded)$/.test(type) || /rebuttal.*(created|recorded)/.test(type)) {
      const id = payloadStringValue(payload, ['rebuttal_id', 'rebuttalId']);
      if (id) earliest(index.rebuttal, id, seq);
    }
    if (/^evidence\.(recorded|available)$/.test(type) || /evidence.*(recorded|available)/.test(type)) {
      const id = payloadStringValue(payload, ['evidence_id', 'evidenceId', 'id']);
      if (id) earliest(index.evidence, id, seq);
    }
    if (/^outcome\.(recorded|evaluated)$/.test(type) || /outcome.*(recorded|evaluated)/.test(type)) {
      const id = payloadStringValue(payload, ['outcome_id', 'outcomeId', 'id']);
      if (id) earliest(index.outcome, id, seq);
      const horizon = payloadStringValue(payload, ['horizon_days', 'horizonDays']);
      if (horizon) earliest(index.outcome, `horizon:${horizon}`, seq);
    }
    if (/^decision\.(proposed|recorded)$/.test(type) || /decision.*(proposed|recorded)/.test(type)) {
      if (index.decision === null || seq < index.decision) index.decision = seq;
    }
    if (isGuardEvent(event)) {
      index.guardEvents.push(event);
    }
    if (EXECUTION_EVENT_PATTERN.test(type)) {
      if (index.execution === null || seq < index.execution) index.execution = seq;
    }
    const payloadStatus = payloadStringValue(payload, ['status'])?.toLowerCase() ?? null;
    if (
      /^run\.(completed|failed|cancelled)$/.test(type) ||
      (type === 'status.changed' && payloadStatus !== null && isTerminalRunStatus(payloadStatus))
    ) {
      if (index.terminal === null || seq < index.terminal) index.terminal = seq;
    }
  }

  return index;
}

/**
 * Status at a cursor, inferred from persisted transitions up to it.
 *
 * Before the first transition a run is queued, not "completed" — the final
 * status belongs to the future. At the replay tail the stored summary is the
 * authority, because the event stream may not repeat every status change.
 */
export function runStatusAtCursor(
  events: readonly RunEvent[],
  cursorSeq: number | null,
): RunStatus {
  const visible = eventsUpTo(events, cursorSeq);
  let status: RunStatus = 'queued';
  for (const event of visible) {
    if (!event) continue;
    const type = (event.type ?? '').trim().toLowerCase();
    const payloadStatus = payloadStringValue(event.payload, ['status'])?.toLowerCase() ?? null;
    if (type === 'status.changed' && payloadStatus !== null) {
      status = payloadStatus as RunStatus;
    } else if (type === 'stage.started' && typeof event.stage === 'string') {
      const normalized = event.stage.trim().toLowerCase() as RunStatus;
      if (RUN_STATUS_LABELS[normalized] !== undefined) status = normalized;
    } else if (type === 'claim.created' && (status === 'queued' || status === 'collecting')) {
      status = 'analysing';
    } else if (type === 'rebuttal.created') {
      status = 'rebutting';
    } else if (type === 'decision.proposed') {
      status = 'adjudicating';
    } else if (type === 'guard.evaluated') {
      status = 'validating';
    } else if (type === 'stage.failed') {
      status = 'failed';
    } else if (type === 'run.completed') {
      status = 'completed';
    }
  }
  return status;
}

export interface ResolvedGuard {
  guard: GuardComparison | null;
  visible: boolean;
  guardId: string | null;
  eventSequence: number | null;
  /** True when only the guard event's payload could be resolved. */
  partial: boolean;
  unavailableFields: string[];
}

export interface AsOfHiddenCounts {
  claims: number;
  rebuttals: number;
  evidence: number;
  outcomes: number;
}

function guardCollection(detail: DecisionDetailPayload | null): GuardComparison[] {
  if (!detail) return [];
  const collection = detail.guard_evaluations ?? detail.guards;
  const rows = Array.isArray(collection) ? collection.filter(Boolean) : [];
  if (rows.length > 0) return rows;
  return detail.guard ? [detail.guard] : [];
}

function guardFromEventPayload(
  event: RunEvent,
  guardId: string | null,
): { guard: GuardComparison; unavailableFields: string[] } {
  const payload = event.payload ?? {};
  const requested = payloadStringValue(payload, ['requested_quantity', 'requestedQuantity']);
  const allowed = payloadStringValue(payload, ['allowed_quantity', 'allowedQuantity']);
  const rule = payloadStringValue(payload, ['binding_rule', 'bindingRule']);
  const status = payloadStringValue(payload, ['status']) ?? 'unknown';
  const quoteTime = payloadStringValue(payload, ['quote_time', 'quoteTime']);
  const policyVersion = payloadStringValue(payload, ['policy_version', 'policyVersion']);
  const unavailableFields = ['reason_codes', 'checks', 'quote_retrieved_at', 'created_at'];
  if (requested === null) unavailableFields.push('requested_quantity');
  if (allowed === null) unavailableFields.push('allowed_quantity');
  if (rule === null) unavailableFields.push('binding_rule');
  if (quoteTime === null) unavailableFields.push('quote_time');
  return {
    guard: {
      id: guardId,
      status,
      requested_quantity: requested,
      allowed_quantity: allowed,
      binding_rule: rule,
      reason_codes: [],
      checks: [],
      quote_time: quoteTime,
      policy_version: policyVersion,
      partial: true,
      unavailable_fields: unavailableFields,
    },
    unavailableFields,
  };
}

/**
 * The guard evaluation visible at the replay cursor.
 *
 * The latest guard event at or before the cursor identifies which evaluation to
 * show. Before any guard event there is no card at all - a proposal is not a
 * check. When the identifier resolves against the API's evaluation collection
 * the full record (status, quantities, rule, quote time and checks) is shown;
 * a legacy event with no resolvable identifier is shown only for the fields it
 * recorded, with the rest named as unavailable. At the tail, a run with no
 * guard event at all may fall back to the stored summary, which is what a
 * finished view has always displayed.
 */
export function resolveVisibleGuard(
  detail: DecisionDetailPayload | null,
  visibleEvents: readonly RunEvent[],
  atEnd: boolean,
): ResolvedGuard {
  const hidden: ResolvedGuard = {
    guard: null,
    visible: false,
    guardId: null,
    eventSequence: null,
    partial: false,
    unavailableFields: [],
  };
  if (!detail) return hidden;

  const guardEvents = sortEvents(visibleEvents.filter(isGuardEvent));
  const latest = guardEvents[guardEvents.length - 1];
  if (!latest) {
    if (atEnd && detail.guard) {
      return {
        guard: detail.guard,
        visible: true,
        guardId: presentText(detail.guard.id ?? null),
        eventSequence: null,
        partial: false,
        unavailableFields: [],
      };
    }
    return hidden;
  }

  const guardId = payloadStringValue(latest.payload, ['guard_id', 'guardId', 'id']);
  const byId = new Map<string, GuardComparison>();
  for (const row of guardCollection(detail)) {
    const id = presentText(row?.id ?? null);
    if (id) byId.set(id, row);
  }
  if (guardId !== null && byId.has(guardId)) {
    return {
      guard: byId.get(guardId) ?? null,
      visible: true,
      guardId,
      eventSequence: latest.seq,
      partial: false,
      unavailableFields: [],
    };
  }

  const partial = guardFromEventPayload(latest, guardId);
  return {
    guard: partial.guard,
    visible: true,
    guardId,
    eventSequence: latest.seq,
    partial: true,
    unavailableFields: partial.unavailableFields,
  };
}

export interface AsOfPresentation {
  /** The detail payload reduced to what was persisted at `cursorSeq`. */
  detail: DecisionDetailPayload;
  cursorSeq: number | null;
  /** True when the cursor is at (or beyond) the stored tail. */
  atEnd: boolean;
  atStart: boolean;
  status: RunStatus;
  verdictVisible: boolean;
  guardVisible: boolean;
  /** The guard evaluation resolved at this cursor, or null before the first check. */
  guard: GuardComparison | null;
  guardId: string | null;
  guardPartial: boolean;
  /** False until an execution event has been reached (or at the tail). */
  executionVisible: boolean;
  outcomesVisible: boolean;
  visibleClaimIds: string[];
  visibleRebuttalIds: string[];
  visibleEvidenceIds: string[];
  hidden: AsOfHiddenCounts;
  /** The final rationale sentence, when a caller wants to pin it outside the scene. */
  finalSummary: string | null;
}

/**
 * Build the one replay-scene state shared by claim cards, rebuttals, the
 * verdict, the guard and the timeline.
 *
 * `cursorSeq === undefined` means "no replay is active": the final payload is
 * returned unchanged. A numeric cursor filters every stage-backed section by
 * the persisted event that produced it, so the scene cannot show a verdict
 * before the decision was recorded. `cursorSeq === null` is the start of the
 * replay and shows only the run shell.
 *
 * At the very end of the log the stored detail is authoritative; there is no
 * future left to hide. Before that point, content with no corresponding event is
 * withheld rather than guessed at. A summary that is intentionally pinned by
 * the caller is returned separately as `finalSummary` and never injected into
 * the scene.
 */
export function buildAsOfPresentation(
  detail: DecisionDetailPayload | null,
  events: readonly RunEvent[],
  cursorSeq: number | null | undefined,
): AsOfPresentation | null {
  if (!detail || !detail.run) return null;

  const eventList = Array.isArray(events) ? events.filter(Boolean) : [];
  const last = lastEventSequence(eventList);
  const atEnd = cursorSeq === undefined ? true : last === null || (cursorSeq ?? -1) >= last;
  const atStart = cursorSeq === null;
  const visibleEvents = eventsUpTo(eventList, cursorSeq);
  const index = indexEventVisibility(visibleEvents);
  const resolvedGuard = resolveVisibleGuard(detail, visibleEvents, atEnd);
  const executionVisible = atEnd || index.execution !== null;

  const claims = Array.isArray(detail.claims) ? detail.claims.filter(Boolean) : [];
  const rebuttals = Array.isArray(detail.rebuttals) ? detail.rebuttals.filter(Boolean) : [];
  const evidence = Array.isArray(detail.evidence) ? detail.evidence.filter(Boolean) : [];
  const outcomes = Array.isArray(detail.outcomes) ? detail.outcomes.filter(Boolean) : [];

  const visibleClaims = atEnd
    ? claims
    : claims.filter((claim) => {
        const id = presentText(claim?.claim_id);
        return id !== null && index.claim.has(id);
      });
  const visibleClaimIds = new Set(
    visibleClaims.map((claim) => presentText(claim?.claim_id)).filter((id): id is string => id !== null),
  );

  const visibleRebuttals = atEnd
    ? rebuttals
    : rebuttals.filter((rebuttal) => {
        const id = presentText(rebuttal?.rebuttal_id);
        return id !== null && index.rebuttal.has(id);
      });

  const visibleEvidence = atEnd
    ? evidence
    : evidence.filter((item) => {
        const id = presentText(item?.id);
        return id !== null && index.evidence.has(id);
      });

  const verdictVisible =
    index.decision !== null
      ? true
      : atEnd &&
        (detail.rationale !== null ||
          detail.action !== null ||
          (Array.isArray(detail.decisive_claim_ids) && detail.decisive_claim_ids.length > 0));

  const guardVisible = resolvedGuard.visible;

  const visibleOutcomes = atEnd
    ? outcomes
    : outcomes.filter((outcome) => {
        const id = presentText((outcome as { id?: string }).id);
        if (id !== null && index.outcome.has(id)) return true;
        // Some producers omit outcome ids from the event payload; the horizon
        // is then the only join key available, and it still has to be recorded.
        const horizon = typeof outcome?.horizon_days === 'number' ? String(outcome.horizon_days) : null;
        return horizon !== null && index.outcome.has(`horizon:${horizon}`);
      });

  const status = atEnd ? detail.run.status : runStatusAtCursor(eventList, cursorSeq ?? null);
  const terminalStatus = isTerminalRunStatus(status);

  const reduced = {
    ...detail,
    run: {
      ...detail.run,
      status,
      completed_at: terminalStatus ? detail.run.completed_at : null,
      latency_ms: terminalStatus ? detail.run.latency_ms : null,
      error_code: status === detail.run.status ? detail.run.error_code : null,
      // Execution is an event too. Before the first execution event the final
      // payload's outcome belongs to the future; it is not shown early.
      execution_state: executionVisible ? detail.run.execution_state ?? null : null,
      execution: executionVisible ? detail.run.execution ?? null : null,
    },
    rationale: verdictVisible ? detail.rationale : null,
    decisive_claim_ids: verdictVisible
      ? atEnd
        ? Array.isArray(detail.decisive_claim_ids)
          ? detail.decisive_claim_ids
          : []
        : (Array.isArray(detail.decisive_claim_ids) ? detail.decisive_claim_ids : []).filter((id) =>
            visibleClaimIds.has(id),
          )
      : [],
    strongest_objection: verdictVisible ? detail.strongest_objection : null,
    action: verdictVisible ? (detail.action ?? null) : null,
    claims: visibleClaims,
    rebuttals: visibleRebuttals,
    evidence: visibleEvidence,
    guard: guardVisible ? resolvedGuard.guard : null,
    outcomes: visibleOutcomes,
    has_rebuttals: atEnd ? detail.has_rebuttals : visibleRebuttals.length > 0,
  };

  return {
    detail: reduced,
    cursorSeq: cursorSeq ?? null,
    atEnd,
    atStart,
    status,
    verdictVisible,
    guardVisible,
    guard: resolvedGuard.guard,
    guardId: resolvedGuard.guardId,
    guardPartial: resolvedGuard.partial,
    executionVisible,
    outcomesVisible: atEnd || visibleOutcomes.length > 0 || index.outcome.size > 0,
    visibleClaimIds: [...visibleClaimIds],
    visibleRebuttalIds: visibleRebuttals
      .map((rebuttal) => presentText(rebuttal?.rebuttal_id))
      .filter((id): id is string => id !== null),
    visibleEvidenceIds: visibleEvidence
      .map((item) => presentText(item?.id))
      .filter((id): id is string => id !== null),
    hidden: {
      claims: claims.length - visibleClaims.length,
      rebuttals: rebuttals.length - visibleRebuttals.length,
      evidence: evidence.length - visibleEvidence.length,
      outcomes: outcomes.length - visibleOutcomes.length,
    },
    finalSummary: firstSentence(detail.rationale),
  };
}
