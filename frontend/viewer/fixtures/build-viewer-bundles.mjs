#!/usr/bin/env node
/**
 * Build the offline viewer's fixture bundle from the committed decision-run
 * manifest.
 *
 * The manifest is the authoritative fixture source. This script does not
 * re-author its prose or outcomes; it joins the timeline steps into the same
 * detail/event shapes the API serves, assigns deterministic ids, and wraps the
 * result in a versioned, checksummed, redacted bundle.
 *
 * Two checks happen before anything is written:
 *   1. every fixture's own manifest checksum still matches, and
 *   2. the produced per-run and envelope checksums are recomputed from the
 *      canonical JSON that the browser will validate.
 */

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(here, '../../..');
const manifestPath = path.join(projectRoot, 'docs/spec/fixtures/decision-runs.v1.json');
const outputPath = path.resolve(here, '../public/decision-runs.v1.bundle.json');

const BUNDLE_VERSION = '1.0';
const SCHEMA_VERSION = '1.0';
const BUNDLE_KIND = 'alphaagent.offline-viewer';

function sortedValue(value) {
  if (Array.isArray(value)) return value.map(sortedValue);
  if (value !== null && typeof value === 'object') {
    const sorted = {};
    for (const key of Object.keys(value).sort()) sorted[key] = sortedValue(value[key]);
    return sorted;
  }
  return value;
}

function canonicalJson(value) {
  return JSON.stringify(sortedValue(value));
}

function sha256(value) {
  return crypto.createHash('sha256').update(value, 'utf8').digest('hex');
}

function contentHash(value) {
  return `sha256:${sha256(canonicalJson(value))}`;
}

function checksumOf(container) {
  const payload = {};
  for (const [key, value] of Object.entries(container)) {
    if (key !== 'checksum') payload[key] = value;
  }
  return sha256(canonicalJson(payload));
}

function withChecksum(container, scope) {
  return {
    ...container,
    checksum: {
      algorithm: 'sha256',
      scope,
      value: checksumOf(container),
    },
  };
}

function requireString(value, field) {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new Error(`Fixture value ${field} must be a non-empty string.`);
  }
  return value.trim();
}

function idText(value) {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return null;
}

function outcomeSignalScore(step) {
  const diagnostics = step.signal_diagnostics;
  if (diagnostics && typeof diagnostics === 'object' && diagnostics.signal_score != null) {
    return String(diagnostics.signal_score);
  }
  if (step.maturity === 'evaluated' && step.decision_return_pct != null) {
    return String(step.decision_return_pct);
  }
  return null;
}

function firstObservationValue(observation) {
  if (!observation || typeof observation !== 'object') return null;
  for (const value of Object.values(observation)) {
    if (typeof value === 'string' || typeof value === 'number') return String(value);
  }
  return null;
}

function buildRun(fixture) {
  const fixtureId = requireString(fixture.fixture_id, 'fixture_id');
  const evidenceByKey = new Map();
  const claimsByKey = new Map();
  const evidence = [];
  const claims = [];
  const rebuttals = [];
  const outcomes = [];
  const events = [];
  let decision = null;
  let guard = null;
  let status = 'queued';
  let seq = 0;
  let lastAt = fixture.started_at;

  const pushEvent = (type, stage, at, agentRole, payload) => {
    seq += 1;
    events.push({
      event_id: `${fixtureId}:event:${seq}`,
      seq,
      type,
      stage,
      agent_role: agentRole ?? null,
      occurred_at: at,
      payload: payload ?? {},
    });
  };

  for (const step of fixture.timeline) {
    const at = requireString(step.at, 'timeline[].at');
    lastAt = at;

    if (step.kind === 'transition') {
      status = step.status;
      pushEvent('status.changed', step.status, at, 'system', {
        status: step.status,
        ...(step.payload ?? {}),
      });
      continue;
    }

    if (step.kind === 'event') {
      pushEvent(step.event_type, step.stage, at, step.agent_role, step.payload);
      continue;
    }

    if (step.kind === 'evidence') {
      const key = requireString(step.key, 'evidence.key');
      const id = `ev-${fixtureId}-${key}`;
      const observationValue = firstObservationValue(step.observation);
      const observation =
        observationValue === null
          ? null
          : {
              value: observationValue,
              unit: step.units ?? '',
              currency: step.currency ?? null,
            };
      const item = {
        id,
        source_type: requireString(step.source_type, 'evidence.source_type'),
        source_uri: step.source_uri ?? null,
        publisher: step.publisher ?? null,
        published_at: step.published_at ?? null,
        retrieved_at: requireString(step.retrieved_at, 'evidence.retrieved_at'),
        excerpt: step.excerpt ?? null,
        observation,
        content_hash: contentHash({
          fixture_id: fixtureId,
          key,
          source_type: step.source_type,
          source_uri: step.source_uri ?? null,
          publisher: step.publisher ?? null,
          published_at: step.published_at ?? null,
          excerpt: step.excerpt ?? null,
          observation: step.observation ?? null,
        }),
      };
      evidence.push(item);
      evidenceByKey.set(key, item);
      pushEvent('evidence.recorded', 'collecting', at, 'system', {
        evidence_id: id,
        key,
        source_type: item.source_type,
        published_at: item.published_at,
        available_at: step.available_at ?? item.retrieved_at,
      });
      continue;
    }

    if (step.kind === 'claim') {
      const key = requireString(step.key, 'claim.key');
      const evidenceIds = (step.evidence_keys ?? []).map((evidenceKey) => {
        const item = evidenceByKey.get(evidenceKey);
        if (!item) throw new Error(`Fixture ${fixtureId} claim ${key} cites unknown evidence ${evidenceKey}.`);
        return item.id;
      });
      const claim = {
        claim_id: `claim-${fixtureId}-${key}`,
        agent_role: requireString(step.agent_role, 'claim.agent_role'),
        stance: step.stance ?? step.agent_role,
        statement: requireString(step.statement, 'claim.statement'),
        claim_type: requireString(step.claim_type, 'claim.claim_type'),
        evidence_ids: evidenceIds,
        horizon: step.horizon ?? null,
        limitations: step.limitations ?? null,
      };
      claims.push(claim);
      claimsByKey.set(key, claim);
      pushEvent('claim.created', 'analysing', at, claim.agent_role, {
        claim_id: claim.claim_id,
        key,
        stance: claim.stance,
        claim_type: claim.claim_type,
        evidence_ids: evidenceIds,
      });
      continue;
    }

    if (step.kind === 'rebuttal') {
      const key = requireString(step.key, 'rebuttal.key');
      const target = claimsByKey.get(step.target_claim_key);
      if (!target) {
        throw new Error(`Fixture ${fixtureId} rebuttal ${key} targets unknown claim ${step.target_claim_key}.`);
      }
      const evidenceIds = (step.evidence_keys ?? []).map((evidenceKey) => {
        const item = evidenceByKey.get(evidenceKey);
        if (!item) throw new Error(`Fixture ${fixtureId} rebuttal ${key} cites unknown evidence ${evidenceKey}.`);
        return item.id;
      });
      const rebuttal = {
        rebuttal_id: `rebuttal-${fixtureId}-${key}`,
        responding_agent: requireString(step.responding_agent, 'rebuttal.responding_agent'),
        target_claim_id: target.claim_id,
        response_type: requireString(step.response_type, 'rebuttal.response_type'),
        explanation: requireString(step.explanation, 'rebuttal.explanation'),
        evidence_ids: evidenceIds,
        position_change: step.position_change ?? null,
      };
      rebuttals.push(rebuttal);
      pushEvent('rebuttal.created', 'rebutting', at, rebuttal.responding_agent, {
        rebuttal_id: rebuttal.rebuttal_id,
        target_claim_id: target.claim_id,
        response_type: rebuttal.response_type,
        key,
      });
      continue;
    }

    if (step.kind === 'decision') {
      const decisiveClaimIds = (step.decisive_claim_keys ?? []).map((claimKey) => {
        const claim = claimsByKey.get(claimKey);
        if (!claim) throw new Error(`Fixture ${fixtureId} decision cites unknown claim ${claimKey}.`);
        return claim.claim_id;
      });
      decision = {
        action: requireString(step.action, 'decision.action'),
        rationale: step.concise_rationale ?? null,
        decisive_claim_ids: decisiveClaimIds,
        strongest_objection: step.strongest_objection ?? null,
      };
      pushEvent('decision.proposed', 'adjudicating', at, 'cio', {
        action: decision.action,
        requested_quantity: step.requested_quantity ?? null,
        decisive_claim_ids: decisiveClaimIds,
        strongest_objection: decision.strongest_objection,
      });
      continue;
    }

    if (step.kind === 'guard') {
      if (!decision) throw new Error(`Fixture ${fixtureId} evaluates a guard before a decision.`);
      guard = {
        status: requireString(step.status, 'guard.status'),
        requested_quantity: String(step.requested_quantity),
        allowed_quantity: String(step.allowed_quantity),
        binding_rule: step.binding_rule ?? null,
        reason_codes: Array.isArray(step.reason_codes) ? step.reason_codes : [],
        checks: Array.isArray(step.checks) ? step.checks : [],
      };
      pushEvent('guard.evaluated', 'validating', at, 'risk', {
        guard_id: `guard-${fixtureId}`,
        status: guard.status,
        requested_quantity: guard.requested_quantity,
        allowed_quantity: guard.allowed_quantity,
        binding_rule: guard.binding_rule,
        reason_codes: guard.reason_codes,
      });
      continue;
    }

    if (step.kind === 'outcome') {
      if (!decision) throw new Error(`Fixture ${fixtureId} records an outcome before a decision.`);
      const outcome = {
        id: `outcome-${fixtureId}-${step.horizon_days}d-${step.method_version}`,
        horizon_days: step.horizon_days,
        maturity: step.maturity,
        signal_score: outcomeSignalScore(step),
        benchmark: step.benchmark_ticker ?? null,
        portfolio_impact: null,
        method_version: step.method_version,
        unavailable_reason: step.unavailable_reason ?? null,
        decision_return_pct: step.decision_return_pct ?? null,
      };
      outcomes.push(outcome);
      pushEvent('outcome.recorded', 'completed', at, 'system', {
        outcome_id: outcome.id,
        horizon_days: outcome.horizon_days,
        maturity: outcome.maturity,
        method_version: outcome.method_version,
      });
      continue;
    }

    throw new Error(`Fixture ${fixtureId} has unknown timeline kind ${step.kind}.`);
  }

  if (!decision) throw new Error(`Fixture ${fixtureId} produced no decision.`);

  const terminal = ['completed', 'failed', 'cancelled'].includes(status);
  if (!terminal) throw new Error(`Fixture ${fixtureId} did not end in a terminal status.`);

  // One explicit terminal event. The status transition above already carries
  // the state; this is the event the replay uses to distinguish "the run is
  // over" from "the last stage completed".
  pushEvent('run.completed', status, lastAt, 'system', {
    status,
    note: 'Fixture playback contains stored events only; no model or execution call was made.',
  });

  const startedAt = requireString(fixture.started_at, 'started_at');
  const latencyMs = Math.max(0, Date.parse(lastAt) - Date.parse(startedAt));
  const mode = requireString(fixture.mode, 'mode');
  const protocolVersion = requireString(fixture.protocol_version, 'protocol_version');
  const ticker = requireString(fixture.ticker, 'ticker');

  const detail = {
    run: {
      id: fixtureId,
      ticker,
      status,
      mode,
      protocol_version: protocolVersion,
      started_at: startedAt,
      completed_at: lastAt,
      latency_ms: latencyMs,
      error_code: null,
      is_synthetic: mode === 'synthetic',
      source: fixture.source,
      fixture_id: fixtureId,
    },
    action: decision.action,
    rationale: decision.rationale,
    decisive_claim_ids: decision.decisive_claim_ids,
    strongest_objection: decision.strongest_objection,
    claims,
    rebuttals,
    evidence,
    guard,
    outcomes,
    has_rebuttals: rebuttals.length > 0,
  };

  const sourceLabel =
    mode === 'recorded' || mode === 'live'
      ? 'real_recorded'
      : mode === 'heuristic'
        ? 'heuristic'
        : mode === 'legacy'
          ? 'legacy'
          : 'synthetic';

  return withChecksum(
    {
      run_id: fixtureId,
      mode,
      source_label: sourceLabel,
      provenance: {
        label: sourceLabel,
        note:
          fixture.label ??
          `Fixture ${fixtureId} from docs/spec/fixtures/decision-runs.v1.json.`,
        fixture_id: fixtureId,
        protocol_version: protocolVersion,
        authored: true,
      },
      detail,
      events,
    },
    "this run object with its 'checksum' member removed, canonicalised as sorted-key compact JSON (UTF-8)",
  );
}

function main() {
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  if (manifest.manifest_version !== 1 || manifest.schema_version !== SCHEMA_VERSION) {
    throw new Error(
      `Unsupported fixture manifest ${manifest.manifest_version}/${manifest.schema_version}; expected 1/${SCHEMA_VERSION}.`,
    );
  }

  // Recompute the manifest's own per-fixture checksums before trusting it.
  for (const fixture of manifest.fixtures) {
    const { checksum, ...payload } = fixture;
    const computed = sha256(canonicalJson(payload));
    if (computed !== checksum) {
      throw new Error(
        `Manifest checksum mismatch for ${fixture.fixture_id}: ${computed} != ${checksum}. Refusing to build a viewer from a modified fixture.`,
      );
    }
  }

  const runs = manifest.fixtures.map(buildRun);
  const sourceLabel = runs.every((run) => run.source_label === 'synthetic')
    ? 'synthetic'
    : runs[0]?.source_label ?? 'synthetic';

  const bundle = withChecksum(
    {
      bundle_kind: BUNDLE_KIND,
      bundle_version: BUNDLE_VERSION,
      schema_version: SCHEMA_VERSION,
      generated_at: new Date().toISOString(),
      generated_for: 'REP-02 offline decision replay viewer',
      source: {
        label: sourceLabel,
        manifest: 'docs/spec/fixtures/decision-runs.v1.json',
        manifest_version: manifest.manifest_version,
        description:
          manifest.honesty?.why ??
          'Committed decision-run fixtures. Every run is labelled synthetic.',
        offline: true,
        read_only: true,
      },
      honesty: {
        ...(manifest.honesty ?? {}),
        no_live_analysis: true,
        no_execution: true,
        no_database: true,
        no_network: true,
        note:
          'The viewer replays these stored payloads only. It has no execution, approval, model or live-analysis capability.',
      },
      runs,
    },
    "this bundle object with its 'checksum' member removed (run checksums included), canonicalised as sorted-key compact JSON (UTF-8)",
  );

  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(outputPath, `${JSON.stringify(bundle, null, 2)}\n`, 'utf8');

  console.log(`WROTE ${path.relative(projectRoot, outputPath)}`);
  console.log(`  runs: ${runs.length}`);
  console.log(`  source label: ${sourceLabel}`);
  for (const run of runs) {
    console.log(
      `  - ${run.run_id}: mode=${run.mode}, events=${run.events.length}, claims=${run.detail.claims.length}, rebuttals=${run.detail.rebuttals.length}, guard=${run.detail.guard ? run.detail.guard.status : 'none'}`,
    );
  }
  console.log(`  bundle checksum: ${bundle.checksum.value}`);
}

main();
