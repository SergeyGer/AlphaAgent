import { describe, expect, it } from 'vitest';
import type { FeedItem } from '../types';
import { deriveAction, mergeFeed, normalizeDebateText, stageLabel } from './feed';

/**
 * The thoughts feed.
 *
 * `mergeFeed` overlays live WebSocket rows on the persisted REST log. Two things
 * about it are easy to get subtly wrong and hard to notice: a row arriving over
 * the socket and then again from REST must collapse to one entry rather than
 * appearing twice, and the ordering must hold across both sources. A duplicate in
 * the feed looks like the agent made the same decision twice, which is a
 * considerably worse impression than a cosmetic bug.
 */

const item = (key: string, timestamp: string, overrides: Partial<FeedItem> = {}): FeedItem => ({
  key,
  kind: 'decision',
  timestamp,
  ticker: null,
  action: null,
  sentiment: null,
  tokensUsed: null,
  apiCostUsd: null,
  reasoning: key,
  bullCase: null,
  bearCase: null,
  stage: null,
  message: null,
  ...overrides,
});

describe('mergeFeed', () => {
  it('orders the combined feed newest first', () => {
    const base = [item('a', '2026-10-07T10:00:00Z'), item('b', '2026-10-07T12:00:00Z')];
    const live = [item('c', '2026-10-07T11:00:00Z')];

    expect(mergeFeed(base, live).map((i) => i.key)).toEqual(['b', 'c', 'a']);
  });

  it('collapses a row that arrives over both transports', () => {
    // The same decision is pushed live and later returned by REST. It must
    // appear once, or the feed reads as the agent acting twice.
    const base = [item('log-1', '2026-10-07T10:00:00Z')];
    const live = [item('log-1', '2026-10-07T10:00:00Z')];

    expect(mergeFeed(base, live)).toHaveLength(1);
  });

  it('prefers the live copy of a duplicated row', () => {
    const base = [item('log-1', '2026-10-07T10:00:00Z', { reasoning: 'from REST' })];
    const live = [item('log-1', '2026-10-07T10:00:00Z', { reasoning: 'from socket' })];

    expect(mergeFeed(base, live)[0]?.reasoning).toBe('from socket');
  });

  it('caps the result so the feed cannot grow without bound', () => {
    const many = Array.from({ length: 150 }, (_, i) =>
      item(`k${i}`, new Date(Date.UTC(2026, 9, 7, 0, i)).toISOString()),
    );

    expect(mergeFeed(many, [], 100)).toHaveLength(100);
  });

  it('keeps the newest rows when capping', () => {
    const many = Array.from({ length: 10 }, (_, i) =>
      item(`k${i}`, new Date(Date.UTC(2026, 9, 7, 0, i)).toISOString()),
    );

    const capped = mergeFeed(many, [], 3);

    expect(capped.map((i) => i.key)).toEqual(['k9', 'k8', 'k7']);
  });

  it('handles empty inputs', () => {
    expect(mergeFeed([], [])).toEqual([]);
    expect(mergeFeed([item('a', '2026-10-07T10:00:00Z')], [])).toHaveLength(1);
  });

  it('does not let an unparseable timestamp throw or corrupt the order', () => {
    const base = [item('good', '2026-10-07T10:00:00Z'), item('bad', 'not-a-date')];

    expect(() => mergeFeed(base, [])).not.toThrow();
    // An unparseable row sorts last rather than landing arbitrarily in the middle.
    expect(mergeFeed(base, []).at(-1)?.key).toBe('bad');
  });
});

describe('deriveAction', () => {
  it('classifies the phrasings the agent actually produces', () => {
    expect(deriveAction('BUY 3 AAPL').label).toBe('BUY');
    expect(deriveAction('Purchased 10 shares').label).toBe('BUY');
    expect(deriveAction('SELL 2 BTC').label).toBe('SELL');
    expect(deriveAction('Trimmed position').label).toBe('SELL');
    expect(deriveAction('HOLD - no execution required.').label).toBe('HOLD');
  });

  it('labels a refusal as BLOCKED rather than as an action', () => {
    // "Blocked by daily loss limit" is a decision the system made, and showing
    // it as a generic ACTION would hide the most important thing on the row.
    expect(deriveAction('Blocked by daily loss limit').label).toBe('BLOCKED');
    expect(deriveAction('HALT - daily loss limit breached').label).toBe('BLOCKED');
  });

  it('handles an absent action without throwing', () => {
    expect(deriveAction(null).label).toBe('N/A');
    expect(deriveAction('').label).toBe('N/A');
  });
});

describe('normalizeDebateText', () => {
  it('treats whitespace-only debate text as absent', () => {
    // The panels test for a truthy string to decide whether to render at all, so
    // "   " must not read as a recorded argument.
    expect(normalizeDebateText('   ')).toBeNull();
    expect(normalizeDebateText('')).toBeNull();
    expect(normalizeDebateText(null)).toBeNull();
    expect(normalizeDebateText(undefined)).toBeNull();
  });

  it('trims and preserves real content', () => {
    expect(normalizeDebateText('  bull case  ')).toBe('bull case');
  });
});

describe('stageLabel', () => {
  it('names each stage the backend emits', () => {
    expect(stageLabel('analyst_started')).toBe('Analyst · started');
    expect(stageLabel('analyst_finished')).toBe('Analyst · finished');
    expect(stageLabel('cio_started')).toBe('CIO · started');
    expect(stageLabel('cio_finished')).toBe('CIO · finished');
  });

  it('falls back to a readable label when no stage is present', () => {
    expect(stageLabel(null)).toBe('Agent');
  });
});
