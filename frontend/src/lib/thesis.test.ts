import { describe, expect, it } from 'vitest';
import { currentThesisFromLogs } from './thesis';
import type { DecisionLog } from '../types';

/**
 * The current thesis is not exposed by any endpoint yet.
 *
 * These tests pin the placeholder's contract: it must return `null` rather than
 * promote the newest decision log to "the thesis", because that would put an
 * invented claim on the dashboard — the log says what the agent did once, not
 * what it currently believes or which positions that belief covers.
 */
describe('currentThesisFromLogs', () => {
  const log: DecisionLog = {
    id: 42,
    action_taken: 'BUY',
    market_sentiment: 'BULLISH',
    ticker: 'AAPL',
    transaction_id: null,
    tokens_used: 1200,
    input_tokens: 900,
    output_tokens: 300,
    cached_input_tokens: 0,
    cache_write_tokens: 0,
    api_cost_usd: '0.01',
    reasoning: 'The setup is asymmetric.',
    bull_case: 'Demand is accelerating.',
    bear_case: 'Valuation is stretched.',
    created_at: '2026-10-14T13:00:00Z',
  };

  it('does not pass a decision log off as the current thesis', () => {
    expect(currentThesisFromLogs([log])).toBeNull();
  });

  it('returns null for an empty or missing log list', () => {
    expect(currentThesisFromLogs([])).toBeNull();
    expect(currentThesisFromLogs(null)).toBeNull();
    expect(currentThesisFromLogs(undefined)).toBeNull();
  });
});
