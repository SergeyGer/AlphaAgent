import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  BundleValidationError,
  canonicalJson,
  parseViewerBundle,
  parseViewerBundleText,
  sha256Hex,
  sourceLabelFromMode,
  sourceLabelForRun,
  SOURCE_LABEL_TEXT,
  type ViewerBundle,
} from './bundle';

/**
 * REP-02 import contract.
 *
 * A viewer that renders a corrupt file is worse than one that refuses it: the
 * reviewer would have no way to tell the replay from a fabrication. These tests
 * pin the refusals first (schema, envelope checksum, per-run checksum) and only
 * then the happy path.
 */

function fixtureBundle(): ViewerBundle {
  const file = path.resolve(process.cwd(), 'viewer/public/decision-runs.v1.bundle.json');
  return JSON.parse(fs.readFileSync(file, 'utf8')) as ViewerBundle;
}

function exportSample(mode = 'recorded') {
  const run = {
    id: 'export-run-1',
    ticker: 'NVDA',
    status: 'completed',
    mode,
    protocol_version: 'debate.v1',
    started_at: '2026-11-10T14:09:40Z',
    completed_at: '2026-11-10T14:10:05Z',
    latency_ms: 25_000,
    error_code: null,
  };
  const payload = {
    bundle_version: '1.0',
    bundle_kind: 'alphaagent.decision-run',
    exported_at: '2026-11-11T10:00:00Z',
    run,
    evidence: [
      {
        id: 'ev-1',
        source_type: 'filing',
        source_uri: 'fixture://sample',
        publisher: 'Sample',
        published_at: '2026-11-09T10:00:00Z',
        retrieved_at: '2026-11-10T14:09:41Z',
        excerpt: 'Sample excerpt.',
        observation: null,
        content_hash: 'sha256:sample',
      },
    ],
    cases: {
      bull: [
        {
          claim_id: 'claim-bull-1',
          agent_role: 'bull',
          stance: 'bull',
          statement: 'Demand is growing.',
          claim_type: 'observed_fact',
          evidence_ids: ['ev-1'],
          horizon: '2 quarters',
          limitations: null,
        },
      ],
      bear: [],
    },
    rebuttals: [],
    verdict: {
      action: 'BUY',
      concise_rationale: 'Demand supports the position.',
      decisive_claim_ids: ['claim-bull-1'],
      strongest_objection: null,
    },
    guard_comparison: null,
    guards: [],
    outcomes: [],
    events: [
      {
        sequence: 1,
        event_id: 'evt-1',
        event_type: 'claim.created',
        stage: 'analysing',
        agent_role: 'bull',
        occurred_at: '2026-11-10T14:09:50Z',
        payload: { claim_id: 'claim-bull-1' },
      },
      {
        sequence: 2,
        event_id: 'evt-2',
        event_type: 'decision.proposed',
        stage: 'adjudicating',
        agent_role: 'cio',
        occurred_at: '2026-11-10T14:10:00Z',
        payload: { action: 'BUY' },
      },
    ],
    redaction: { evidence_body_policy: 'Excerpts only when redistributable.' },
  };
  return {
    ...payload,
    checksum: {
      algorithm: 'sha256',
      scope: 'this object with the checksum member removed',
      value: sha256Hex(canonicalJson(payload)),
    },
  };
}

describe('sha256 + canonical JSON', () => {
  it('matches known SHA-256 test vectors', () => {
    expect(sha256Hex('')).toBe(
      'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
    );
    expect(sha256Hex('abc')).toBe(
      'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
    );
  });

  it('canonicalises object keys without whitespace', () => {
    expect(canonicalJson({ b: 1, a: [2, { d: 4, c: 3 }] })).toBe(
      '{"a":[2,{"c":3,"d":4}],"b":1}',
    );
  });
});

describe('viewer bundle validation', () => {
  it('imports the committed fixture bundle with explicit synthetic labels', () => {
    const bundle = parseViewerBundle(fixtureBundle());
    expect(bundle.kind).toBe('offline-viewer');
    expect(bundle.bundleVersion).toBe('1.0');
    expect(bundle.sourceLabel).toBe('synthetic');
    expect(bundle.runs).toHaveLength(3);
    expect(bundle.runs.every((run) => run.source_label === 'synthetic')).toBe(true);
    expect(bundle.runs.map((run) => run.detail.run.ticker)).toEqual(['AAPL', 'TSLA', 'MSFT']);
    expect(bundle.runs.find((run) => run.detail.rebuttals.length > 0)?.detail.has_rebuttals).toBe(
      true,
    );
    expect(bundle.runs.some((run) => run.detail.guard?.status === 'clamped')).toBe(true);
    expect(bundle.runs.some((run) => run.detail.outcomes.some((o) => o.maturity === 'unavailable'))).toBe(
      true,
    );
    // Event ids in the detail are the same ids the replay joins on.
    for (const run of bundle.runs) {
      const claimIds = new Set(run.detail.claims.map((claim) => claim.claim_id));
      for (const event of run.events) {
        if (event.type === 'claim.created') {
          expect(claimIds.has(String(event.payload.claim_id))).toBe(true);
        }
      }
    }
  });

  it('rejects an unsupported schema version before importing anything', () => {
    const bundle = fixtureBundle() as unknown as Record<string, unknown>;
    bundle['schema_version'] = '2.0';
    expect(() => parseViewerBundle(bundle)).toThrow(BundleValidationError);
    try {
      parseViewerBundle(bundle);
    } catch (cause) {
      expect((cause as BundleValidationError).code).toBe('unsupported-schema');
      expect((cause as Error).message).toContain('Unsupported schema version');
      expect((cause as Error).message).toContain('2.0');
    }
  });

  it('rejects a corrupted envelope checksum with the exact reason', () => {
    const bundle = fixtureBundle();
    bundle.runs[0]!.detail.rationale = 'A modified rationale that no longer matches the checksum.';
    expect(() => parseViewerBundle(bundle)).toThrow(BundleValidationError);
    try {
      parseViewerBundle(bundle);
    } catch (cause) {
      expect((cause as BundleValidationError).code).toBe('checksum');
      expect((cause as Error).message).toContain('Checksum mismatch');
      expect((cause as Error).message).toContain('not imported');
    }
  });

  it('rejects a corrupted per-run checksum even when the envelope is recomputed', () => {
    const bundle = fixtureBundle();
    bundle.runs[0]!.detail.claims[0]!.statement = 'Tampered claim text.';
    const { checksum: _checksum, ...payload } = bundle as unknown as Record<string, unknown>;
    bundle.checksum = {
      algorithm: 'sha256',
      scope: 'recomputed by the test',
      value: sha256Hex(canonicalJson(payload)),
    };

    try {
      parseViewerBundle(bundle);
      throw new Error('expected refusal');
    } catch (cause) {
      expect(cause).toBeInstanceOf(BundleValidationError);
      expect((cause as BundleValidationError).code).toBe('checksum');
      expect((cause as Error).message).toContain('run 1');
    }
  });

  it('rejects an unsupported bundle type', () => {
    expect(() => parseViewerBundle({ bundle_kind: 'something-else' })).toThrow(
      BundleValidationError,
    );
  });

  it('rejects malformed JSON with a readable message', () => {
    expect(() => parseViewerBundleText('{not json')).toThrow('not valid JSON');
  });
});

describe('API decision-run export adapter', () => {
  it('imports a real-recorded-style export and maps its events/detail', () => {
    const bundle = parseViewerBundle(exportSample('recorded'));
    expect(bundle.kind).toBe('decision-run-export');
    expect(bundle.sourceLabel).toBe('real_recorded');
    expect(bundle.runs).toHaveLength(1);

    const run = bundle.runs[0]!;
    expect(run.source_label).toBe('real_recorded');
    expect(SOURCE_LABEL_TEXT[run.source_label]).toBe('Real recorded');
    expect(run.detail.run.id).toBe('export-run-1');
    expect(run.detail.claims).toHaveLength(1);
    expect(run.detail.rationale).toBe('Demand supports the position.');
    expect(run.detail.action).toBe('BUY');
    expect(run.detail.decisive_claim_ids).toEqual(['claim-bull-1']);
    expect(run.events.map((event) => event.seq)).toEqual([1, 2]);
    expect(run.events[0]?.type).toBe('claim.created');
    expect(run.events[1]?.type).toBe('decision.proposed');
  });

  it('labels a synthetic export synthetic, not recorded', () => {
    const bundle = parseViewerBundle(exportSample('synthetic'));
    expect(bundle.sourceLabel).toBe('synthetic');
    expect(bundle.runs[0]?.source_label).toBe('synthetic');
    expect(SOURCE_LABEL_TEXT[bundle.sourceLabel]).toBe('Synthetic');
  });

  it('rejects an export whose checksum was corrupted', () => {
    const sample = exportSample('recorded') as unknown as Record<string, unknown>;
    (sample['run'] as Record<string, unknown>)['ticker'] = 'TSLA';
    expect(() => parseViewerBundle(sample)).toThrow(BundleValidationError);
  });
});

describe('source labels', () => {
  it('maps every mode to an explicit label', () => {
    expect(sourceLabelFromMode('recorded')).toBe('real_recorded');
    expect(sourceLabelFromMode('live')).toBe('real_recorded');
    expect(sourceLabelFromMode('synthetic')).toBe('synthetic');
    expect(sourceLabelFromMode('heuristic')).toBe('heuristic');
    expect(sourceLabelFromMode('legacy')).toBe('legacy');
  });

  it('prefers the explicit bundle label over the mode fallback', () => {
    expect(
      sourceLabelForRun({ mode: 'recorded', source_label: 'synthetic' }),
    ).toBe('synthetic');
    expect(
      sourceLabelForRun({ mode: 'synthetic', source_label: 'real_recorded' }),
    ).toBe('real_recorded');
  });
});


describe('guard collection import', () => {
  it('preserves every guard evaluation from an API export', () => {
    const raw = exportSample() as unknown as Record<string, unknown>;
    const payload: Record<string, unknown> = { ...raw };
    delete payload['checksum'];
    const guards = [
      {
        id: 'guard-preflight',
        status: 'approved',
        requested_quantity: '10',
        allowed_quantity: '10',
        binding_rule: 'max_trade_allocation_pct',
        reason_codes: [],
        checks: [],
        quote_time: '2026-11-10T14:09:59Z',
      },
      {
        id: 'guard-final',
        status: 'rejected',
        requested_quantity: '10',
        allowed_quantity: '0',
        binding_rule: 'approval_terms',
        reason_codes: ['approval_terms_changed'],
        checks: [],
        quote_time: '2026-11-10T14:10:04Z',
      },
    ];
    payload['guards'] = guards;
    payload['guard_evaluations'] = guards;
    payload['guard_comparison'] = guards[1];
    const events = (payload['events'] as Array<Record<string, unknown>>) ?? [];
    events.push({
      sequence: 3,
      event_id: 'evt-guard-preflight',
      event_type: 'guard.evaluated',
      stage: 'validating',
      agent_role: 'system',
      occurred_at: '2026-11-10T14:09:59Z',
      payload: { guard_id: 'guard-preflight', status: 'approved' },
    });
    events.push({
      sequence: 4,
      event_id: 'evt-guard-final',
      event_type: 'guard.evaluated',
      stage: 'validating',
      agent_role: 'system',
      occurred_at: '2026-11-10T14:10:04Z',
      payload: { guard_id: 'guard-final', status: 'rejected' },
    });
    payload['events'] = events;

    const bundle = parseViewerBundle({
      ...payload,
      checksum: {
        algorithm: 'sha256',
        scope: 'this object with the checksum member removed',
        value: sha256Hex(canonicalJson(payload)),
      },
    });

    const detail = bundle.runs[0]?.detail;
    expect(detail?.guard_evaluations?.map((row) => row.id)).toEqual([
      'guard-preflight',
      'guard-final',
    ]);
    expect(detail?.guards?.map((row) => row.id)).toEqual(['guard-preflight', 'guard-final']);
    // The summary field remains the most recent evaluation for finished views.
    expect(detail?.guard?.id).toBe('guard-final');
  });
});
