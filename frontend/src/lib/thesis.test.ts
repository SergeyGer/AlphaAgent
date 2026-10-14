import { describe, expect, it } from 'vitest';
import { thesisFor, thesisFromRow } from './thesis';
import type { ThesisRow } from '../types';

/**
 * The thesis view model.
 *
 * The state that matters most here is **absent**: a position with no recorded
 * thesis must render as "nothing on record" rather than as an empty thesis. It
 * is a real state — the position was opened before theses existed, or by an
 * operator-approved trade — and the agent is told the same thing, so the UI must
 * not manufacture a claim the backend never made.
 */

const row = (over: Partial<ThesisRow> = {}): ThesisRow => ({
  id: 1,
  ticker: 'AAPL',
  thesis: 'Services margin expanding.',
  invalidation: 'Gross margin below 40%.',
  status: 'OPEN',
  opened_at: '2026-10-14T13:00:00Z',
  updated_at: '2026-10-14T13:00:00Z',
  closed_at: null,
  close_reason: '',
  opened_by_decision: 1490,
  ...over,
});

describe('thesisFromRow', () => {
  it('maps the wire row onto the view model', () => {
    const thesis = thesisFromRow(row());

    expect(thesis.ticker).toBe('AAPL');
    expect(thesis.rationale).toBe('Services margin expanding.');
    expect(thesis.invalidation).toBe('Gross margin below 40%.');
    expect(thesis.decisionId).toBe(1490);
    expect(thesis.statedAt).toBe('2026-10-14T13:00:00Z');
  });

  it('treats a whitespace-only invalidation as none recorded', () => {
    // The UI tests `invalidation` for truthiness to decide whether to explain
    // that the agent named no falsifier, so "   " must not read as one.
    expect(thesisFromRow(row({ invalidation: '   ' })).invalidation).toBe('');
    expect(thesisFromRow(row({ invalidation: '' })).invalidation).toBe('');
  });

  it('survives a pruned decision reference', () => {
    expect(thesisFromRow(row({ opened_by_decision: null })).decisionId).toBeNull();
  });
});

describe('thesisFor', () => {
  it('finds the thesis for an instrument, case-insensitively', () => {
    const rows = [row({ ticker: 'AAPL' }), row({ id: 2, ticker: 'TSLA', thesis: 'other' })];

    expect(thesisFor(rows, 'aapl')?.rationale).toBe('Services margin expanding.');
    expect(thesisFor(rows, 'TSLA')?.rationale).toBe('other');
  });

  it('returns null when the instrument has no thesis on record', () => {
    // Not an empty thesis: a position can legitimately have none.
    expect(thesisFor([row({ ticker: 'AAPL' })], 'NVDA')).toBeNull();
  });

  it('returns null for an empty, missing or absent payload', () => {
    expect(thesisFor([], 'AAPL')).toBeNull();
    expect(thesisFor(null, 'AAPL')).toBeNull();
    expect(thesisFor(undefined, 'AAPL')).toBeNull();
  });

  it('returns null rather than matching everything when no ticker is given', () => {
    expect(thesisFor([row()], null)).toBeNull();
    expect(thesisFor([row()], '')).toBeNull();
    expect(thesisFor([row()], undefined)).toBeNull();
  });

  it('does not match a ticker on a partial string', () => {
    // "AA" must not match "AAPL"; a substring match would show the wrong thesis
    // against the wrong position, which is worse than showing none.
    expect(thesisFor([row({ ticker: 'AAPL' })], 'AA')).toBeNull();
  });
});
