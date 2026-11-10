import type {
  Claim,
  ClaimType,
  DecisionDetailPayload,
  EvidenceItem,
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

/** Filter events for replay. `undefined` means "show everything". */
export function eventsUpTo(events: readonly RunEvent[], cursorSeq: number | null | undefined): RunEvent[] {
  if (cursorSeq === undefined) return [...events];
  const cutoff = cursorSeq === null ? -1 : cursorSeq;
  return events.filter((event) => typeof event?.seq === 'number' && event.seq <= cutoff);
}

/** The sequence number of the final event, or `null` when there are none. */
export function lastEventSequence(events: readonly RunEvent[]): number | null {
  const sequences = eventSequences(events);
  return sequences.length === 0 ? null : (sequences[sequences.length - 1] ?? null);
}

export function isAtReplayEnd(events: readonly RunEvent[], cursorSeq: number | null): boolean {
  const last = lastEventSequence(events);
  return last === null || cursorSeq === last;
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
