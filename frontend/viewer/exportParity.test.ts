import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  BundleValidationError,
  canonicalJson,
  parseViewerBundle,
  parseViewerBundleText,
  sha256Hex,
  type ImportedBundle,
} from './bundle';

/**
 * Step 5B — cross-language parity.
 *
 * Both this suite and `core/tests/test_p1_export_portability.py` read
 * `docs/spec/fixtures/canonical-json.v1.json`. The vectors contain nested
 * objects with reordered keys, decimal strings, nullable times, Unicode and
 * JSON numbers whose Python spelling (90.0) differs from the JavaScript one
 * (90). If either implementation changes alone, one of the two suites fails.
 */

const projectRoot = path.resolve(process.cwd(), '..');
const vectorPath = path.join(projectRoot, 'docs/spec/fixtures/canonical-json.v1.json');
const samplePath = path.join(projectRoot, 'docs/spec/fixtures/decision-run-export.v1.json');
const supersededPath = path.join(
  projectRoot,
  'docs/spec/fixtures/decision-run-export.v0-superseded.json',
);
const providerPath = path.join(
  projectRoot,
  'docs/spec/fixtures/decision-run-export.provider-backed.v1.json',
);
const fixtureBundlePath = path.resolve(
  process.cwd(),
  'viewer/public/decision-runs.v1.bundle.json',
);

function readJson(file: string): Record<string, unknown> {
  return JSON.parse(fs.readFileSync(file, 'utf8')) as Record<string, unknown>;
}

interface VectorDocument {
  vector_version: string;
  vectors: Array<{ name: string; value: unknown; canonical: string; sha256: string }>;
}

function importSample(): Record<string, unknown> {
  return readJson(samplePath);
}

function importSuperseded(): Record<string, unknown> {
  return readJson(supersededPath);
}

function importProvider(): Record<string, unknown> {
  return readJson(providerPath);
}

function expectValidationError(run: () => unknown, code: string): BundleValidationError {
  try {
    run();
    throw new Error('expected the viewer to refuse the document');
  } catch (cause) {
    expect(cause).toBeInstanceOf(BundleValidationError);
    const error = cause as BundleValidationError;
    expect(error.code).toBe(code);
    return error;
  }
}

describe('shared canonical JSON vectors', () => {
  const document = readJson(vectorPath) as unknown as VectorDocument;

  it('declares version 1.0 and carries vectors', () => {
    expect(document.vector_version).toBe('1.0');
    expect(document.vectors.length).toBeGreaterThanOrEqual(6);
  });

  it('produces the declared canonical text and SHA-256 for every vector', () => {
    for (const vector of document.vectors) {
      expect(canonicalJson(vector.value), vector.name).toBe(vector.canonical);
      expect(sha256Hex(canonicalJson(vector.value)), vector.name).toBe(vector.sha256);
    }
  });

  it('hashes 90.0 and 90 identically, as Python does', () => {
    expect(canonicalJson({ v: 90.0 })).toBe('{"v":90}');
    expect(canonicalJson({ v: 90 })).toBe('{"v":90}');
    expect(sha256Hex(canonicalJson({ v: 90.0 }))).toBe(sha256Hex(canonicalJson({ v: 90 })));
  });

  it('handles a nested payload with reordered keys exactly like the fixture', () => {
    const nested = document.vectors.find((vector) => vector.name === 'nested-and-reordered');
    expect(nested).toBeDefined();
    expect(canonicalJson(nested?.value)).toBe(nested?.canonical);
  });
});

describe('importing the captured API export', () => {
  it('imports the committed sample unchanged with no network', () => {
    const fetchCalls: unknown[] = [];
    const originalFetch = globalThis.fetch;
    // "Networking disabled": any fetch during import fails the test.
    globalThis.fetch = ((...args: unknown[]) => {
      fetchCalls.push(args);
      throw new Error('The offline viewer must not call the network.');
    }) as typeof globalThis.fetch;
    try {
      const sample = importSample();
      const imported: ImportedBundle = parseViewerBundle(sample);
      expect(fetchCalls).toEqual([]);
      expect(imported.kind).toBe('decision-run-export');
      expect(imported.bundleVersion).toBe('1.0');
      expect(imported.schemaVersion).toBe('1.0');
      expect(imported.sourceLabel).toBe('synthetic');
      expect(imported.runs).toHaveLength(1);

      const run = imported.runs[0]!;
      expect(run.raw_export).toEqual(sample);
      expect(run.source_label).toBe('synthetic');
      expect(run.exported_at).toMatch(/Z$/);
      expect(run.provenance.protocol_version).toBe('synthetic.v1');
      expect(run.detail.run.status).toBe('completed');
      expect(run.detail.run.mode).toBe('synthetic');
      expect(run.detail.run.source).toBe('fixture');
      expect(run.detail.run.error_code).toBeNull();
      // The authored sample must not be presented as a provider debate.
      expect(run.provenance.origin_kind).toBe('authored_fixture');
      expect(run.provenance.provider_backed).toBe(false);
      expect(run.provenance.note).toContain('Authored fixture content');
      expect(run.provenance.limitations?.length).toBeGreaterThan(0);
      // The full metadata envelope survives, not just the fields the list
      // endpoint happens to publish.
      expect(run.detail.run.cost).toBeTruthy();
      expect(run.detail.run.stage_states).toBeTruthy();
      expect(run.detail.run.protocol).toBeTruthy();
      expect(run.detail.run.failure).toBeNull();
      expect(run.detail.run.provenance).toMatchObject({
        mode: 'synthetic',
        is_synthetic: true,
        source: 'fixture',
      });
      const claims = (sample['provenance_claims'] ?? {}) as Record<string, unknown>;
      const build = claims['build_verification'] as Record<string, unknown>;
      expect(build['establishes_provider_debate']).toBe(false);
      const integrity = claims['integrity'] as Record<string, unknown>;
      expect(integrity['establishes_integrity']).toBe(true);
      expect(integrity['establishes_provider_debate']).toBe(false);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it('imports the superseded sample as authored, not as a provider debate', () => {
    const fetchCalls: unknown[] = [];
    const originalFetch = globalThis.fetch;
    globalThis.fetch = ((...args: unknown[]) => {
      fetchCalls.push(args);
      throw new Error('The offline viewer must not call the network.');
    }) as typeof globalThis.fetch;
    try {
      const sample = importSuperseded();
      const imported = parseViewerBundle(sample);
      expect(fetchCalls).toEqual([]);
      expect(imported.sourceLabel).toBe('synthetic');
      expect(imported.runs[0]?.source_label).toBe('synthetic');
      expect(imported.runs[0]?.provenance.origin_kind).toBeNull();
      expect(imported.runs[0]?.provenance.note).toContain('Superseded export label');
      expect(imported.runs[0]?.provenance.note).toContain('fixture_id');
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it('rejects a modified superseded payload', () => {
    const tampered = importSuperseded();
    (tampered['run'] as Record<string, unknown>)['mode'] = 'live';
    expectValidationError(() => parseViewerBundle(tampered), 'checksum');
  });

  it('preserves a provider-backed run origin and its limitations', () => {
    const fetchCalls: unknown[] = [];
    const originalFetch = globalThis.fetch;
    globalThis.fetch = ((...args: unknown[]) => {
      fetchCalls.push(args);
      throw new Error('The offline viewer must not call the network.');
    }) as typeof globalThis.fetch;
    try {
      const sample = importProvider();
      const imported = parseViewerBundle(sample);
      expect(fetchCalls).toEqual([]);
      expect(imported.sourceLabel).toBe('real_recorded');
      const run = imported.runs[0]!;
      expect(run.provenance.origin_kind).toBe('provider_run');
      expect(run.provenance.provider_backed).toBe(true);
      expect(run.provenance.limitations?.some((text) => text.includes("provider 'anthropic'"))).toBe(
        true,
      );
      expect(run.provenance.limitations?.some((text) => text.includes('Test data'))).toBe(true);
      expect(run.detail.run.mode).toBe('live');
      expect(run.detail.run.source).toBe('crewai');
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it('preserves every guard evaluation, superseded outcome and the schedule', () => {
    const imported = parseViewerBundle(importSample());
    const detail = imported.runs[0]!.detail;

    expect(detail.guard_evaluations).toHaveLength(2);
    expect(detail.guard_evaluations?.every((row) => typeof row.id === 'string')).toBe(true);
    expect(detail.guard_evaluations?.map((row) => row.status)).toEqual(['approved', 'clamped']);
    expect(detail.guard_evaluations?.[0]?.requested_quantity).toBe('10.00000000');
    expect(detail.guard_evaluations?.[0]?.allowed_quantity).toBe('10.00000000');
    expect(detail.guard_evaluations?.[1]?.requested_quantity).toBe('10.00000000');
    expect(detail.guard_evaluations?.[1]?.allowed_quantity).toBe('4.00000000');
    expect(detail.guard_evaluations?.[0]?.checks).toHaveLength(2);
    expect(detail.guard_evaluations?.[1]?.checks).toHaveLength(2);
    expect(detail.guard?.id).toBe(detail.guard_evaluations?.[1]?.id);
    expect(detail.guards).toEqual(detail.guard_evaluations);

    expect(detail.superseded_outcomes).toHaveLength(1);
    expect(detail.superseded_outcomes?.[0]?.horizon_days).toBe(7);
    expect(detail.superseded_outcomes?.[0]?.method_version).toBe('attribution.v1');
    expect(detail.outcomes.map((row) => row.horizon_days)).toEqual([1, 7, 30]);
    expect(detail.outcomes[0]?.maturity).toBe('unavailable');
    expect(detail.outcome_schedule?.entries).toHaveLength(3);
    expect(detail.outcome_schedule?.pending).toBe(true);

    // The flat consumer contract is derived from the nested verdict.
    expect(detail.action).toBe('BUY');
    expect(detail.rationale).toContain('Résumé — 你好');
    expect(detail.strongest_objection).toBe('The margin expansion may not persist.');
    expect(detail.has_rebuttals).toBe(true);
    expect(detail.rebuttals).toHaveLength(1);
    expect(detail.rebuttals[0]?.response_type).toBe('qualify');
    expect(detail.limitations?.length).toBeGreaterThan(0);
    expect(detail.snapshot?.portfolio_version).toBe('portfolio-v7');
    expect(detail.verdict?.action).toBe('BUY');
  });

  it('keeps evidence identifiers linked and the non-redistributable body omitted', () => {
    const imported = parseViewerBundle(importSample());
    const detail = imported.runs[0]!.detail;
    const evidenceIds = new Set(detail.evidence.map((row) => row.id));
    for (const claim of [...detail.claims]) {
      for (const id of claim.evidence_ids) expect(evidenceIds.has(id)).toBe(true);
    }
    const omitted = detail.evidence.filter(
      (row) => (row as unknown as Record<string, unknown>).excerpt_omitted === true,
    );
    expect(omitted).toHaveLength(1);
    expect(omitted[0]?.excerpt).toBeNull();
    expect(omitted[0]?.published_at).toBeNull();
  });

  it('preserves event extras and the event sequence', () => {
    const imported = parseViewerBundle(importSample());
    const events = imported.runs[0]!.events;
    expect(events.map((event) => event.seq)).toEqual(
      [...events.map((event) => event.seq)].sort((a, b) => a - b),
    );
    const floatEvent = events.find(
      (event) =>
        (event.payload as Record<string, unknown>)['unicode'] === 'ünïcode 你好',
    );
    expect(floatEvent).toBeDefined();
    expect((floatEvent?.payload as Record<string, unknown>)['settings']).toMatchObject({
      max_position_pct: 90.0,
      timeout_seconds: 300.0,
      slippage: 0.0025,
    });
    expect(floatEvent?.schema_version).toBeTruthy();
    expect(floatEvent?.persisted_at).toBeTruthy();
  });

  it('rejects a semantic edit in the stored payload', () => {
    const tampered = importSample();
    const guards = tampered['guard_evaluations'] as Array<Record<string, unknown>>;
    guards[1]!['allowed_quantity'] = '5.00000000';
    const error = expectValidationError(() => parseViewerBundle(tampered), 'checksum');
    expect(error.message).toContain('Checksum mismatch');
    expect(error.message).toContain('not imported');
  });

  it('rejects an unsupported schema version by name and value', () => {
    const future = importSample();
    future['schema_version'] = '2.0';
    const error = expectValidationError(() => parseViewerBundle(future), 'unsupported-schema');
    expect(error.message).toContain('schema version');
    expect(error.message).toContain('"2.0"');

    const oldVersion = importSample();
    oldVersion['bundle_version'] = '0.9';
    const versionError = expectValidationError(
      () => parseViewerBundle(oldVersion),
      'unsupported-schema',
    );
    expect(versionError.message).toContain('bundle version');
    expect(versionError.message).toContain('"0.9"');
  });

  it('rejects malformed JSON and a wrong bundle kind with a concrete reason', () => {
    expect(() => parseViewerBundleText('{not json')).toThrow('not valid JSON');
    const wrongKind = importSample();
    wrongKind['bundle_kind'] = 'alphaagent.something-else';
    expectValidationError(() => parseViewerBundle(wrongKind), 'unsupported-kind');
  });
});

describe('committed synthetic fixtures under their supported format', () => {
  it('still opens all three runs as synthetic', () => {
    const bundle = parseViewerBundleText(fs.readFileSync(fixtureBundlePath, 'utf8'));
    expect(bundle.kind).toBe('offline-viewer');
    expect(bundle.schemaVersion).toBe('1.0');
    expect(bundle.runs).toHaveLength(3);
    expect(bundle.runs.every((run) => run.source_label === 'synthetic')).toBe(true);
    expect(bundle.runs.some((run) => run.detail.guard?.status === 'clamped')).toBe(true);
  });
});
