import type { AgentStage, AgentThinkingEvent, DecisionLog, FeedItem } from '../types';

const STAGE_LABELS: Record<AgentStage, string> = {
  analyst_started: 'Analyst · started',
  analyst_finished: 'Analyst · finished',
  cio_started: 'CIO · started',
  cio_finished: 'CIO · finished',
};

export function stageLabel(stage: AgentStage | null): string {
  if (!stage) return 'Agent';
  return STAGE_LABELS[stage] ?? stage;
}

export type ActionTone = 'emerald' | 'rose' | 'slate' | 'sky';

export interface ActionDescriptor {
  label: string;
  tone: ActionTone;
}

/**
 * Condense the free-text `action_taken` sentence into a compact side badge
 * (e.g. "Purchased 0.5 BTC @ $83000.00" → BUY).
 */
export function deriveAction(action: string | null): ActionDescriptor {
  const text = (action ?? '').toLowerCase();
  if (text.trim() === '') return { label: 'N/A', tone: 'slate' };
  if (/purchas|bought|buy|accumulat|adding|added/.test(text)) return { label: 'BUY', tone: 'emerald' };
  if (/sold|sell|liquidat|trimmed|exit|closed/.test(text)) return { label: 'SELL', tone: 'rose' };
  if (/block|rejected|denied|halted/.test(text)) return { label: 'BLOCKED', tone: 'rose' };
  if (/hold|no action|no trade|wait|skip|monitor/.test(text)) return { label: 'HOLD', tone: 'slate' };
  return { label: 'ACTION', tone: 'sky' };
}

/** Convert a persisted decision log into a feed row. */
export function feedItemFromLog(log: DecisionLog): FeedItem {
  return {
    key: `log-${log.id}`,
    kind: 'decision',
    timestamp: log.created_at,
    ticker: log.ticker,
    action: log.action_taken,
    sentiment: log.market_sentiment,
    tokensUsed: typeof log.tokens_used === 'number' ? log.tokens_used : null,
    apiCostUsd: log.api_cost_usd,
    reasoning: log.reasoning,
    bullCase: log.bull_case,
    bearCase: log.bear_case,
    stage: null,
    message: null,
  };
}

/** Convert a live `agent.thinking` frame into a feed row. */
export function feedItemFromThinking(event: AgentThinkingEvent): FeedItem {
  return {
    key: `thinking-${event.at}-${event.ticker}-${event.stage}`,
    kind: 'thinking',
    timestamp: event.at,
    ticker: event.ticker,
    action: null,
    sentiment: null,
    tokensUsed: null,
    apiCostUsd: null,
    reasoning: null,
    bullCase: null,
    bearCase: null,
    stage: event.stage,
    message: event.message,
  };
}

/** Trim a debate text field, collapsing `""`/whitespace/null to `null`. */
export function normalizeDebateText(value: string | null | undefined): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  return trimmed === '' ? null : trimmed;
}

function timestampOf(item: FeedItem): number {
  const parsed = Date.parse(item.timestamp);
  return Number.isNaN(parsed) ? 0 : parsed;
}

/**
 * Overlay live WebSocket rows on top of the persisted REST log, newest first.
 * Rows are de-duplicated by `key` (so a live decision log collapses into the
 * REST row once it is fetched) and the result is capped in memory.
 */
export function mergeFeed(base: FeedItem[], live: FeedItem[], cap = 100): FeedItem[] {
  const merged = new Map<string, FeedItem>();
  for (const item of base) merged.set(item.key, item);
  for (const item of live) merged.set(item.key, item);

  return Array.from(merged.values())
    .sort((a, b) => timestampOf(b) - timestampOf(a))
    .slice(0, cap);
}
