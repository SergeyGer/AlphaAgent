#!/usr/bin/env node
/**
 * Export review for bundles that leave the server / enter the public viewer.
 *
 * Key-name redaction is not enough. A credential can be pasted into a rationale,
 * an internal URL can sit in an evidence excerpt, and an email address can
 * appear in a free-text limitation. Every string is therefore scanned, and
 * every URL is parsed and scheme-checked:
 *
 *   - `fixture:` is the only external-looking scheme the public bundles use;
 *   - `http(s)`, `ws(s)`, `ftp`, `file`, `data` and `mailto` are refused;
 *   - provider/secret patterns and loopback hostnames are refused anywhere,
 *     including inside prose.
 *
 * The scanner is intentionally strict. A false positive is a reason to reword
 * the fixture, not to weaken the export.
 */

import fs from 'node:fs';
import path from 'node:path';

const SENSITIVE_KEY =
  /^(?:account|account_id|api[_-]?key|apikey|authorization|auth|chat_id|credential|credentials|email|legacy_log_id|password|passwd|portfolio|portfolio_id|secret|telegram|telegram_username|token|user|user_id|username)$/i;

const STRING_PATTERNS = [
  { label: 'provider API key', pattern: /\bsk-[A-Za-z0-9_-]{16,}\b/g },
  { label: 'AWS access key', pattern: /\bAKIA[0-9A-Z]{16}\b/g },
  { label: 'GitHub token', pattern: /\bgh[pousr]_[A-Za-z0-9]{20,}\b/g },
  { label: 'Slack token', pattern: /\bxox[a-z]-[A-Za-z0-9-]{10,}\b/g },
  { label: 'bearer token', pattern: /\bBearer\s+[A-Za-z0-9._~+/-]{12,}/gi },
  {
    label: 'credential assignment',
    pattern:
      /\b(?:api[_-]?key|access[_-]?token|secret|password|passwd|authorization)\b\s*[:=]\s*["']?[A-Za-z0-9._~+/-]{8,}/gi,
  },
  {
    label: 'email address',
    pattern: /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi,
  },
  {
    label: 'http(s) URL in free text',
    pattern: /\bhttps?:\/\/[^\s"'<>]+/gi,
  },
  {
    label: 'websocket URL in free text',
    pattern: /\bwss?:\/\/[^\s"'<>]+/gi,
  },
  {
    label: 'file URL in free text',
    pattern: /\bfile:\/\/[^\s"'<>]+/gi,
  },
  {
    label: 'loopback / bind-all host',
    pattern: /\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d{1,5})?\b/gi,
  },
  {
    label: 'absolute developer path',
    pattern: /\b(?:\/root|\/home|\/Users)\/[^\s"'<>]+/g,
  },
];

const ALLOWED_URL_SCHEMES = new Set(['fixture']);

function collectFindings(value, pointer, findings, key = '') {
  if (typeof value === 'string') {
    if (key && SENSITIVE_KEY.test(key)) {
      findings.push({ pointer, label: `sensitive key "${key}"` });
    }

    const trimmed = value.trim();
    if (/^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed)) {
      const scheme = (trimmed.match(/^([a-z][a-z0-9+.-]*):\/\//i)?.[1] ?? '').toLowerCase();
      if (!ALLOWED_URL_SCHEMES.has(scheme)) {
        findings.push({
          pointer,
          label: `URL scheme "${scheme}:" is not allowed in an offline bundle`,
          match: trimmed.slice(0, 160),
        });
      }
    }

    for (const { label, pattern } of STRING_PATTERNS) {
      pattern.lastIndex = 0;
      for (const match of value.matchAll(pattern)) {
        findings.push({ pointer, label, match: String(match[0]).slice(0, 160) });
      }
    }
    return;
  }

  if (Array.isArray(value)) {
    value.forEach((item, index) => collectFindings(item, `${pointer}[${index}]`, findings, key));
    return;
  }

  if (value !== null && typeof value === 'object') {
    for (const [childKey, child] of Object.entries(value)) {
      if (SENSITIVE_KEY.test(childKey)) {
        findings.push({ pointer: `${pointer}.${childKey}`, label: `sensitive key "${childKey}"` });
      }
      collectFindings(child, `${pointer}.${childKey}`, findings, childKey);
    }
  }
}

function main() {
  const target = process.argv[2] ?? 'viewer/public/decision-runs.v1.bundle.json';
  const resolved = path.resolve(process.cwd(), target);
  if (!fs.existsSync(resolved)) {
    console.error(`REVIEW FAILED: ${target} does not exist.`);
    process.exit(2);
  }

  let parsed;
  try {
    parsed = JSON.parse(fs.readFileSync(resolved, 'utf8'));
  } catch (cause) {
    console.error(`REVIEW FAILED: ${target} is not valid JSON — ${cause.message}`);
    process.exit(2);
  }

  const findings = [];
  collectFindings(parsed, '$', findings);

  if (findings.length > 0) {
    console.error(`REVIEW FAILED: ${findings.length} issue(s) in ${target}`);
    for (const finding of findings) {
      console.error(`  ${finding.pointer}: ${finding.label}${finding.match ? ` → ${finding.match}` : ''}`);
    }
    console.error(
      'Free-text fields and URLs are scanned, not just sensitive key names. Reword the fixture or remove the data.',
    );
    process.exit(1);
  }

  const runCount = Array.isArray(parsed?.runs) ? parsed.runs.length : 1;
  const labels = Array.isArray(parsed?.runs)
    ? [...new Set(parsed.runs.map((run) => run?.source_label ?? 'unknown'))].join(', ')
    : parsed?.run?.mode ?? 'unknown';
  console.log(`REVIEW OK: ${target}`);
  console.log(`  runs: ${runCount}`);
  console.log(`  source label(s): ${labels}`);
  console.log('  free-text and URL scan: no credentials, emails, external URLs or loopback hosts');
}

main();
