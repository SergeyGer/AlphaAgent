import { mergeRunEvents } from '../src/lib/decisionRuns';
import type {
  Claim,
  DecisionDetailPayload,
  EvidenceItem,
  GuardComparison,
  OutcomeView,
  Rebuttal,
  RunEvent,
  RunMode,
  RunStatus,
  RunSummary,
} from '../src/types';

/**
 * REP-02 — versioned, checksummed, redacted bundle import for the offline viewer.
 *
 * Two formats are accepted:
 *
 * 1. `alphaagent.offline-viewer` — the multi-run fixture bundle built from the
 *    committed decision-run manifest. It carries its own per-run checksums.
 * 2. `alphaagent.decision-run` — a single run exported by the authenticated API
 *    (`GET /api/decision-runs/<id>/export/`). A reviewer can capture that file
 *    while the API is running and open it later with no database, no login and
 *    no network.
 *
 * Validation happens before any state is imported: schema version, envelope
 * checksum, per-run checksum, then shape. A corrupted or unsupported file is
 * refused with a concrete message rather than rendered partially.
 */

/* ------------------------------------------------------------------ */
/* SHA-256 (pure JS, so file:// and jsdom work without a secure context) */
/* ------------------------------------------------------------------ */

const SHA256_K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function rotateRight(value: number, bits: number): number {
  return (value >>> bits) | (value << (32 - bits));
}

/** Lowercase hex SHA-256 over the UTF-8 bytes of `input`. */
export function sha256Hex(input: string): string {
  const bytes = new TextEncoder().encode(input);
  const bitLength = bytes.length * 8;
  const padded = new Uint8Array((((bytes.length + 9) + 63) >> 6) << 6);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  const view = new DataView(padded.buffer);
  view.setUint32(padded.length - 8, Math.floor(bitLength / 0x1_0000_0000));
  view.setUint32(padded.length - 4, bitLength >>> 0);

  let h0 = 0x6a09e667;
  let h1 = 0xbb67ae85;
  let h2 = 0x3c6ef372;
  let h3 = 0xa54ff53a;
  let h4 = 0x510e527f;
  let h5 = 0x9b05688c;
  let h6 = 0x1f83d9ab;
  let h7 = 0x5be0cd19;

  const words = new Uint32Array(64);
  for (let offset = 0; offset < padded.length; offset += 64) {
    for (let index = 0; index < 16; index += 1) {
      words[index] = view.getUint32(offset + index * 4);
    }
    for (let index = 16; index < 64; index += 1) {
      const w15 = words[index - 15] ?? 0;
      const w2 = words[index - 2] ?? 0;
      const s0 = rotateRight(w15, 7) ^ rotateRight(w15, 18) ^ (w15 >>> 3);
      const s1 = rotateRight(w2, 17) ^ rotateRight(w2, 19) ^ (w2 >>> 10);
      words[index] = (words[index - 16]! + s0 + words[index - 7]! + s1) >>> 0;
    }

    let a = h0;
    let b = h1;
    let c = h2;
    let d = h3;
    let e = h4;
    let f = h5;
    let g = h6;
    let h = h7;

    for (let index = 0; index < 64; index += 1) {
      const s1 = rotateRight(e, 6) ^ rotateRight(e, 11) ^ rotateRight(e, 25);
      const choice = (e & f) ^ (~e & g);
      const temp1 = (h + s1 + choice + (SHA256_K[index] ?? 0) + (words[index] ?? 0)) >>> 0;
      const s0 = rotateRight(a, 2) ^ rotateRight(a, 13) ^ rotateRight(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (s0 + majority) >>> 0;

      h = g;
      g = f;
      f = e;
      e = (d + temp1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) >>> 0;
    }

    h0 = (h0 + a) >>> 0;
    h1 = (h1 + b) >>> 0;
    h2 = (h2 + c) >>> 0;
    h3 = (h3 + d) >>> 0;
    h4 = (h4 + e) >>> 0;
    h5 = (h5 + f) >>> 0;
    h6 = (h6 + g) >>> 0;
    h7 = (h7 + h) >>> 0;
  }

  return [h0, h1, h2, h3, h4, h5, h6, h7]
    .map((word) => word.toString(16).padStart(8, '0'))
    .join('');
}

/* ------------------------------------------------------------------ */
/* Canonical JSON + checksum                                            */
/* ------------------------------------------------------------------ */

function sortedValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortedValue);
  if (value !== null && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    const sorted: Record<string, unknown> = {};
    for (const key of Object.keys(record).sort()) sorted[key] = sortedValue(record[key]);
    return sorted;
  }
  return value;
}

/** Sorted-key, compact JSON — the canonical form the checksum covers. */
export function canonicalJson(value: unknown): string {
  return JSON.stringify(sortedValue(value));
}

export interface BundleChecksum {
  algorithm?: string;
  scope?: string;
  value?: string;
}

function checksumOf(container: Record<string, unknown>): string {
  const payload: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(container)) {
    if (key !== 'checksum') payload[key] = value;
  }
  return sha256Hex(canonicalJson(payload));
}

/* ------------------------------------------------------------------ */
/* Source labels                                                        */
/* ------------------------------------------------------------------ */

export type ViewerSourceLabel = 'real_recorded' | 'synthetic' | 'heuristic' | 'legacy';

export const VIEWER_SOURCE_LABELS: readonly ViewerSourceLabel[] = [
  'real_recorded',
  'synthetic',
  'heuristic',
  'legacy',
];

export const SOURCE_LABEL_TEXT: Record<ViewerSourceLabel, string> = {
  real_recorded: 'Real recorded',
  synthetic: 'Synthetic',
  heuristic: 'Heuristic',
  legacy: 'Legacy',
};

export const SOURCE_LABEL_DESCRIPTION: Record<ViewerSourceLabel, string> = {
  real_recorded:
    'A recording of a run that actually executed. The viewer replays stored events and never calls a model or the execution path.',
  synthetic:
    'Fixture data authored to exercise the interface. It is not a market decision and no outcome should be inferred from it.',
  heuristic:
    'The deterministic fallback produced this run. No bull/bear debate occurred, so missing claims are absent by construction.',
  legacy:
    'A report recorded before the claim/evidence/rebuttal protocol. Missing sections were never produced in this shape.',
};

export function isViewerSourceLabel(value: unknown): value is ViewerSourceLabel {
  return (
    typeof value === 'string' && (VIEWER_SOURCE_LABELS as readonly string[]).includes(value)
  );
}

export function sourceLabelFromMode(mode: RunMode | string | null | undefined): ViewerSourceLabel {
  const key = typeof mode === 'string' ? mode.trim().toLowerCase() : '';
  if (key === 'recorded' || key === 'live') return 'real_recorded';
  if (key === 'heuristic') return 'heuristic';
  if (key === 'legacy') return 'legacy';
  return 'synthetic';
}

export function sourceLabelForRun(run: Pick<ViewerRun, 'source_label' | 'mode'>): ViewerSourceLabel {
  return isViewerSourceLabel(run.source_label)
    ? run.source_label
    : sourceLabelFromMode(run.mode);
}

/* ------------------------------------------------------------------ */
/* Bundle contract                                                      */
/* ------------------------------------------------------------------ */

export const VIEWER_BUNDLE_KIND = 'alphaagent.offline-viewer';
export const DECISION_EXPORT_KIND = 'alphaagent.decision-run';
export const SUPPORTED_BUNDLE_VERSIONS = ['1.0'] as const;
export const VIEWER_SCHEMA_VERSION = '1.0';

export interface ViewerRunProvenance {
  label: ViewerSourceLabel;
  note: string;
  fixture_id?: string | null;
  protocol_version?: string | null;
  authored?: boolean;
}

export interface ViewerRun {
  run_id: string;
  mode: RunMode;
  source_label: ViewerSourceLabel;
  provenance: ViewerRunProvenance;
  detail: DecisionDetailPayload;
  events: RunEvent[];
  checksum?: BundleChecksum;
}

export interface ViewerBundle {
  bundle_kind: typeof VIEWER_BUNDLE_KIND;
  bundle_version: string;
  schema_version: string;
  generated_at: string;
  source: {
    label: ViewerSourceLabel;
    description: string;
    offline: true;
    read_only: true;
  };
  honesty?: Record<string, unknown>;
  runs: ViewerRun[];
  checksum?: BundleChecksum;
}

export type ImportedBundleKind = 'offline-viewer' | 'decision-run-export';

export interface ImportedBundle {
  kind: ImportedBundleKind;
  bundleVersion: string;
  schemaVersion: string;
  generatedAt: string | null;
  sourceLabel: ViewerSourceLabel;
  description: string;
  honesty: Record<string, unknown> | null;
  runs: ViewerRun[];
}

export type BundleErrorCode =
  | 'unsupported-schema'
  | 'unsupported-kind'
  | 'checksum'
  | 'shape';

export class BundleValidationError extends Error {
  readonly code: BundleErrorCode;

  constructor(code: BundleErrorCode, message: string) {
    super(message);
    this.name = 'BundleValidationError';
    this.code = code;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : null;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && item.trim() !== '')
    : [];
}

function assertSupportedVersion(version: unknown, field: string): string {
  if (typeof version !== 'string' || !SUPPORTED_BUNDLE_VERSIONS.includes(version as '1.0')) {
    throw new BundleValidationError(
      'unsupported-schema',
      `Unsupported ${field} ${typeof version === 'string' ? `"${version}"` : '(missing)'}. ` +
        `This viewer supports ${SUPPORTED_BUNDLE_VERSIONS.join(', ')}.`,
    );
  }
  return version;
}

function verifyChecksum(container: unknown, label: string): void {
  if (!isRecord(container)) {
    throw new BundleValidationError('shape', `${label} is not a JSON object.`);
  }
  const checksum = container['checksum'];
  if (!isRecord(checksum)) {
    throw new BundleValidationError('checksum', `${label} has no checksum; refusing to import it.`);
  }
  const algorithm = text(checksum['algorithm']) ?? 'sha256';
  if (algorithm.toLowerCase() !== 'sha256') {
    throw new BundleValidationError(
      'checksum',
      `${label} uses unsupported checksum algorithm "${algorithm}"; only sha256 is accepted.`,
    );
  }
  const declared = text(checksum['value']);
  if (!declared) {
    throw new BundleValidationError('checksum', `${label} has an empty checksum value.`);
  }
  const normalized = declared.toLowerCase().replace(/^sha256:/, '');
  const computed = checksumOf(container);
  if (normalized !== computed) {
    throw new BundleValidationError(
      'checksum',
      `Checksum mismatch for ${label} — the contents no longer match the recorded SHA-256. ` +
        'The file was modified or corrupted and was not imported.',
    );
  }
}

/* ------------------------------------------------------------------ */
/* Shape validation                                                     */
/* ------------------------------------------------------------------ */

const RUN_MODES: readonly RunMode[] = ['live', 'recorded', 'heuristic', 'legacy', 'synthetic'];
const RUN_STATUSES: readonly RunStatus[] = [
  'queued',
  'collecting',
  'analysing',
  'rebutting',
  'adjudicating',
  'validating',
  'awaiting_approval',
  'completed',
  'failed',
  'cancelled',
];

function normalizedMode(value: unknown): RunMode {
  const key = typeof value === 'string' ? value.trim().toLowerCase() : '';
  if ((RUN_MODES as readonly string[]).includes(key)) return key as RunMode;
  throw new BundleValidationError(
    'shape',
    `Run mode ${typeof value === 'string' ? `"${value}"` : '(missing)'} is not a supported provenance label.`,
  );
}

function normalizedStatus(value: unknown): RunStatus {
  const key = typeof value === 'string' ? value.trim().toLowerCase() : '';
  if ((RUN_STATUSES as readonly string[]).includes(key)) return key as RunStatus;
  throw new BundleValidationError(
    'shape',
    `Run status ${typeof value === 'string' ? `"${value}"` : '(missing)'} is not recognised.`,
  );
}

function validateRunSummary(value: unknown, label: string): RunSummary {
  if (!isRecord(value)) throw new BundleValidationError('shape', `${label} has no run object.`);
  const id = text(value['id']);
  if (!id) throw new BundleValidationError('shape', `${label} has no run id.`);
  return {
    id,
    ticker: text(value['ticker']) ?? 'UNKNOWN',
    status: normalizedStatus(value['status']),
    mode: normalizedMode(value['mode']),
    protocol_version: text(value['protocol_version']) ?? '',
    started_at: text(value['started_at']),
    completed_at: text(value['completed_at']),
    latency_ms: numberOrNull(value['latency_ms']),
    error_code: text(value['error_code']),
    is_synthetic:
      typeof value['is_synthetic'] === 'boolean'
        ? value['is_synthetic']
        : normalizedMode(value['mode']) === 'synthetic',
    source: text(value['source']) ?? undefined,
    legacy_log_id:
      typeof value['legacy_log_id'] === 'string' || typeof value['legacy_log_id'] === 'number'
        ? value['legacy_log_id']
        : null,
    fixture_id: text(value['fixture_id']),
  };
}

function validateRunEvents(value: unknown, label: string): RunEvent[] {
  if (!Array.isArray(value)) throw new BundleValidationError('shape', `${label} has no events array.`);
  const events: RunEvent[] = [];
  for (const [index, raw] of value.entries()) {
    if (!isRecord(raw)) {
      throw new BundleValidationError('shape', `${label} event ${index + 1} is not an object.`);
    }
    const eventId = text(raw['event_id']) ?? `event-${index + 1}`;
    const seq = numberOrNull(raw['seq']);
    if (seq === null || seq < 0) {
      throw new BundleValidationError(
        'shape',
        `${label} event ${index + 1} has no finite sequence number.`,
      );
    }
    events.push({
      event_id: eventId,
      seq,
      type: text(raw['type']) ?? 'unknown',
      stage: text(raw['stage']),
      agent_role: text(raw['agent_role']),
      occurred_at: text(raw['occurred_at']) ?? '',
      payload: isRecord(raw['payload']) ? raw['payload'] : {},
    });
  }
  return mergeRunEvents([], events);
}

function validateViewerRun(value: unknown, index: number): ViewerRun {
  if (!isRecord(value)) {
    throw new BundleValidationError('shape', `Run ${index + 1} is not an object.`);
  }
  verifyChecksum(value, `run ${index + 1}`);

  const detailRaw = value['detail'];
  if (!isRecord(detailRaw)) {
    throw new BundleValidationError('shape', `Run ${index + 1} has no detail payload.`);
  }
  const run = validateRunSummary(detailRaw['run'], `Run ${index + 1}`);
  const events = validateRunEvents(value['events'], `Run ${index + 1}`);
  const mode = normalizedMode(value['mode'] ?? run.mode);
  const sourceLabel = isViewerSourceLabel(value['source_label'])
    ? value['source_label']
    : sourceLabelFromMode(mode);
  const provenanceRaw = isRecord(value['provenance']) ? value['provenance'] : {};

  const detail: DecisionDetailPayload = {
    run: { ...run, mode },
    action: text(detailRaw['action']),
    rationale: text(detailRaw['rationale']),
    decisive_claim_ids: stringArray(detailRaw['decisive_claim_ids']),
    strongest_objection: text(detailRaw['strongest_objection']),
    claims: Array.isArray(detailRaw['claims']) ? (detailRaw['claims'] as Claim[]) : [],
    rebuttals: Array.isArray(detailRaw['rebuttals']) ? (detailRaw['rebuttals'] as Rebuttal[]) : [],
    evidence: Array.isArray(detailRaw['evidence']) ? (detailRaw['evidence'] as EvidenceItem[]) : [],
    guard: (detailRaw['guard'] as GuardComparison | null | undefined) ?? null,
    outcomes: Array.isArray(detailRaw['outcomes']) ? (detailRaw['outcomes'] as OutcomeView[]) : [],
    has_rebuttals: Boolean(detailRaw['has_rebuttals']),
  };

  return {
    run_id: text(value['run_id']) ?? run.id,
    mode,
    source_label: sourceLabel,
    provenance: {
      label: isViewerSourceLabel(provenanceRaw['label']) ? provenanceRaw['label'] : sourceLabel,
      note:
        text(provenanceRaw['note']) ??
        'Provenance note missing from this run; the explicit source label is the only claim available.',
      fixture_id: text(provenanceRaw['fixture_id']),
      protocol_version: text(provenanceRaw['protocol_version']) ?? run.protocol_version,
      authored: provenanceRaw['authored'] === true,
    },
    detail,
    events,
    checksum: isRecord(value['checksum'])
      ? {
          algorithm: text(value['checksum']['algorithm']) ?? 'sha256',
          scope: text(value['checksum']['scope']) ?? undefined,
          value: text(value['checksum']['value']) ?? undefined,
        }
      : undefined,
  };
}

/* ------------------------------------------------------------------ */
/* API export adapter                                                   */
/* ------------------------------------------------------------------ */

function flattenCases(value: unknown): Claim[] {
  if (!isRecord(value)) return [];
  const byId = new Map<string, Claim>();
  for (const group of Object.values(value)) {
    if (!Array.isArray(group)) continue;
    for (const item of group) {
      if (!isRecord(item)) continue;
      const id = text(item['claim_id']);
      if (!id || byId.has(id)) continue;
      byId.set(id, item as unknown as Claim);
    }
  }
  return [...byId.values()];
}

function flattenObjectList(value: unknown): Rebuttal[] {
  return Array.isArray(value) ? (value.filter(isRecord) as unknown as Rebuttal[]) : [];
}

function normalizeExportEvent(value: unknown, index: number): RunEvent {
  if (!isRecord(value)) {
    throw new BundleValidationError('shape', `Export event ${index + 1} is not an object.`);
  }
  return {
    event_id: text(value['event_id']) ?? `export-event-${index + 1}`,
    seq: numberOrNull(value['seq'] ?? value['sequence']) ?? index + 1,
    type: text(value['type'] ?? value['event_type']) ?? 'unknown',
    stage: text(value['stage']),
    agent_role: text(value['agent_role']),
    occurred_at: text(value['occurred_at']) ?? '',
    payload: isRecord(value['payload']) ? value['payload'] : {},
  };
}

function importDecisionExport(raw: Record<string, unknown>): ImportedBundle {
  const version = assertSupportedVersion(raw['bundle_version'], 'bundle version');
  verifyChecksum(raw, 'the decision-run export');

  const run = validateRunSummary(raw['run'], 'the exported run');
  const verdict = isRecord(raw['verdict']) ? raw['verdict'] : {};
  const guardRaw = raw['guard_comparison'] ?? (Array.isArray(raw['guards']) ? raw['guards'][0] : null);
  const guard = isRecord(guardRaw) ? (guardRaw as unknown as GuardComparison) : null;
  const claims = flattenCases(raw['cases']);
  const rebuttals = flattenObjectList(raw['rebuttals']);
  const events = Array.isArray(raw['events'])
    ? mergeRunEvents([], raw['events'].map((event, index) => normalizeExportEvent(event, index)))
    : [];

  const detail: DecisionDetailPayload = {
    run,
    action: text(verdict['action']),
    rationale: text(verdict['concise_rationale']),
    decisive_claim_ids: stringArray(verdict['decisive_claim_ids']),
    strongest_objection: text(verdict['strongest_objection']),
    claims,
    rebuttals,
    evidence: Array.isArray(raw['evidence']) ? (raw['evidence'] as EvidenceItem[]) : [],
    guard,
    outcomes: Array.isArray(raw['outcomes']) ? (raw['outcomes'] as OutcomeView[]) : [],
    has_rebuttals: rebuttals.length > 0,
  };

  const sourceLabel = sourceLabelFromMode(run.mode);
  return {
    kind: 'decision-run-export',
    bundleVersion: version,
    schemaVersion: version,
    generatedAt: text(raw['exported_at']),
    sourceLabel,
    description:
      'Imported AlphaAgent decision-run export. The source label is derived from the run provenance; the viewer cannot independently re-authenticate a recording.',
    honesty: isRecord(raw['redaction']) ? raw['redaction'] : null,
    runs: [
      {
        run_id: run.id,
        mode: run.mode,
        source_label: sourceLabel,
        provenance: {
          label: sourceLabel,
          note:
            text(raw['redaction'] && isRecord(raw['redaction']) ? raw['redaction']['evidence_body_policy'] : null) ??
            'Imported from the authenticated export endpoint. Evidence bodies follow the export redaction policy.',
          fixture_id: run.fixture_id ?? null,
          protocol_version: run.protocol_version,
          authored: false,
        },
        detail,
        events,
        checksum: isRecord(raw['checksum'])
          ? {
              algorithm: text(raw['checksum']['algorithm']) ?? 'sha256',
              scope: text(raw['checksum']['scope']) ?? undefined,
              value: text(raw['checksum']['value']) ?? undefined,
            }
          : undefined,
      },
    ],
  };
}

/* ------------------------------------------------------------------ */
/* Entry point                                                          */
/* ------------------------------------------------------------------ */

export function parseViewerBundle(raw: unknown): ImportedBundle {
  if (!isRecord(raw)) {
    throw new BundleValidationError('shape', 'The bundle is not a JSON object.');
  }
  const kind = text(raw['bundle_kind']);

  if (kind === DECISION_EXPORT_KIND) return importDecisionExport(raw);

  if (kind !== VIEWER_BUNDLE_KIND) {
    throw new BundleValidationError(
      'unsupported-kind',
      `Unsupported bundle type ${kind ? `"${kind}"` : '(missing)'}. Expected "${VIEWER_BUNDLE_KIND}" ` +
        `or "${DECISION_EXPORT_KIND}".`,
    );
  }

  const bundleVersion = assertSupportedVersion(raw['bundle_version'], 'bundle version');
  const schemaVersion = assertSupportedVersion(raw['schema_version'], 'schema version');
  verifyChecksum(raw, 'the viewer bundle');

  if (!Array.isArray(raw['runs']) || raw['runs'].length === 0) {
    throw new BundleValidationError('shape', 'The viewer bundle contains no runs.');
  }
  const runs = raw['runs'].map((run, index) => validateViewerRun(run, index));
  const source = isRecord(raw['source']) ? raw['source'] : {};
  const explicitLabel = isViewerSourceLabel(source['label']) ? source['label'] : null;
  const sourceLabel = explicitLabel ?? runs[0]?.source_label ?? 'synthetic';

  return {
    kind: 'offline-viewer',
    bundleVersion,
    schemaVersion,
    generatedAt: text(raw['generated_at']),
    sourceLabel,
    description:
      text(source['description']) ??
      `${SOURCE_LABEL_TEXT[sourceLabel]} fixture bundle. Read-only, offline replay.`,
    honesty: isRecord(raw['honesty']) ? raw['honesty'] : null,
    runs,
  };
}

/** Parse JSON text and validate it. Throws `BundleValidationError` on refusal. */
export function parseViewerBundleText(value: string): ImportedBundle {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new BundleValidationError('shape', 'The selected file is not valid JSON.');
  }
  return parseViewerBundle(parsed);
}

/** Cheap label used by the UI without importing anything. */
export function bundleSourceLabelText(label: ViewerSourceLabel): string {
  return SOURCE_LABEL_TEXT[label];
}
