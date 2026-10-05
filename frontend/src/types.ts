/**
 * AlphaAgent API + WebSocket contracts.
 *
 * Money / quantity / percentage values arrive from the Django REST API as
 * decimal *strings* (DRF `DecimalField`). They are typed as `string` here and
 * must always be parsed with the helpers in `lib/format.ts` before rendering.
 */

/* ------------------------------------------------------------------ */
/* Primitives & envelopes                                              */
/* ------------------------------------------------------------------ */

/** DRF decimal field — always a string on the wire. */
export type Decimal = string;

/** Standard DRF page envelope. */
export interface Paginated<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

/** A DRF list endpoint may or may not be paginated; accept both. */
export type ListResponse<T> = Paginated<T> | T[];

/** Frozen error envelope: `{ error, status_code, detail, errors }`. */
export interface ApiErrorPayload {
  error?: boolean;
  status_code?: number;
  detail?: string;
  errors?: Record<string, string[] | string> | null;
}

/* ------------------------------------------------------------------ */
/* Auth                                                                */
/* ------------------------------------------------------------------ */

export interface LoginRequest {
  username: string;
  password: string;
}

export interface LoginResponse {
  token: string;
}

/* ------------------------------------------------------------------ */
/* Portfolio                                                           */
/* ------------------------------------------------------------------ */

export interface PortfolioMetrics {
  cash_balance_usd: Decimal;
  positions_value_usd: Decimal;
  total_equity_usd: Decimal;
  invested_cost_usd: Decimal;
  unrealised_pnl_usd: Decimal;
  unrealised_pnl_pct: Decimal;
  realised_pnl_today_usd: Decimal;
  realised_pnl_total_usd: Decimal;
  daily_loss_limit_usd: Decimal;
  daily_loss_used_usd: Decimal;
  daily_loss_remaining_usd: Decimal;
  max_trade_budget_usd: Decimal;
  is_autonomy_blocked: boolean;
  block_reason: string | null;
  unpriced_tickers: string[];
}

export interface Asset {
  id: number;
  ticker: string;
  amount: Decimal;
  avg_purchase_price: Decimal;
  market_price: Decimal;
  price_source: string;
  cost_basis_usd: Decimal;
  market_value_usd: Decimal;
  unrealised_pnl_usd: Decimal;
  unrealised_pnl_pct: Decimal;
  allocation_pct: Decimal;
}

export interface Portfolio {
  id: number;
  username: string;
  balance_usd: Decimal;
  risk_profile: string;
  risk_profile_display: string;
  is_autonomous: boolean;
  max_trade_allocation_pct: Decimal;
  max_trade_budget_usd: Decimal;
  daily_loss_limit_usd: Decimal;
  created_at: string;
  metrics: PortfolioMetrics;
  assets: Asset[];
}

/* ------------------------------------------------------------------ */
/* Snapshots (equity curve)                                            */
/* ------------------------------------------------------------------ */

export interface PortfolioSnapshot {
  captured_at: string;
  total_equity_usd: Decimal;
  cash_balance_usd: Decimal;
  positions_value_usd: Decimal;
  unrealised_pnl_usd: Decimal;
  realised_pnl_today_usd: Decimal;
}

/** Chart range selector. `ALL` omits the `hours` query param. */
export type SnapshotRange = '24H' | '7D' | '30D' | 'ALL';

/* ------------------------------------------------------------------ */
/* Transactions                                                        */
/* ------------------------------------------------------------------ */

export type TxType = 'BUY' | 'SELL';

export interface Transaction {
  id: number;
  ticker: string;
  tx_type: TxType;
  amount: Decimal;
  price: Decimal;
  gross_value_usd: Decimal;
  executed_by: string;
  timestamp: string;
}

/* ------------------------------------------------------------------ */
/* AI decision logs ("thoughts")                                       */
/* ------------------------------------------------------------------ */

export type MarketSentiment = 'BULLISH' | 'BEARISH' | 'NEUTRAL' | string;

export interface DecisionLog {
  id: number;
  action_taken: string;
  market_sentiment: MarketSentiment;
  ticker: string | null;
  transaction_id: number | null;
  tokens_used: number | null;
  api_cost_usd: Decimal | null;
  /** The CIO's verdict — which argument won, and why. */
  reasoning: string | null;
  /**
   * Bullish argument from the research analyst (may be several thousand
   * characters). `""` when the debate was not recorded; null is tolerated too.
   */
  bull_case: string | null;
  /** Bearish argument from the risk assessor / short seller. `""` when absent. */
  bear_case: string | null;
  created_at: string;
}

/** Unified row rendered by the AI thoughts feed (REST logs + live WS frames). */
export interface FeedItem {
  /** Stable React key: `log-<id>` or `thinking-<at>-<ticker>-<stage>`. */
  key: string;
  kind: 'decision' | 'thinking';
  timestamp: string;
  ticker: string | null;
  action: string | null;
  sentiment: MarketSentiment | null;
  tokensUsed: number | null;
  apiCostUsd: Decimal | null;
  reasoning: string | null;
  /** Debate sides for the expandable view; null when not recorded. */
  bullCase: string | null;
  bearCase: string | null;
  stage: AgentStage | null;
  message: string | null;
}

/* ------------------------------------------------------------------ */
/* Recommendations                                                     */
/* ------------------------------------------------------------------ */

export type RecommendationStatus =
  | 'PENDING'
  | 'APPROVED'
  | 'REJECTED'
  | 'EXPIRED'
  | 'EXECUTED'
  | 'BLOCKED';

export type RecommendationAction = 'BUY' | 'SELL';

export interface Recommendation {
  id: number;
  ticker: string;
  action: RecommendationAction;
  amount: Decimal;
  price: Decimal;
  notional_usd: Decimal;
  sentiment: MarketSentiment;
  reasoning: string | null;
  status: RecommendationStatus;
  decided_via: string;
  decided_at: string | null;
  expires_at: string | null;
  created_at: string;
}

export interface RecommendationDecisionResponse {
  id: number;
  status: RecommendationStatus;
  executed: boolean;
  transaction_id: number | null;
  message: string;
}

/* ------------------------------------------------------------------ */
/* News sentiment                                                      */
/* ------------------------------------------------------------------ */

/** Aggregate lexicon sentiment across every scored headline. */
export interface NewsSentimentSummary {
  label: MarketSentiment;
  score: number;
  confidence: number;
  positive_hits: string[];
  negative_hits: string[];
  articles_scored: number;
}

/** One headline with its individual polarity. */
export interface NewsArticle {
  title: string;
  source: string;
  /** May be `""` — render as plain text rather than a link in that case. */
  url: string;
  /** RFC-1123 date string, or `null` when the feed omits the timestamp. */
  published_at: string | null;
  summary: string;
  polarity: number;
  sentiment: MarketSentiment;
}

/** `GET /api/market/news/?ticker=&limit=` — coverage split by sentiment. */
export interface MarketNewsReport {
  ticker: string;
  headline_count: number;
  sentiment: NewsSentimentSummary;
  sources_used: string[];
  /** `true` when every feed was unreachable — all three lists are then empty. */
  degraded: boolean;
  fetched_at: string;
  /** Strongest first. */
  positive: NewsArticle[];
  /** Strongest first (most negative). */
  negative: NewsArticle[];
  neutral: NewsArticle[];
}

/* ------------------------------------------------------------------ */
/* Actions                                                             */
/* ------------------------------------------------------------------ */

export interface ToggleAutonomyResponse {
  portfolio_id: number;
  is_autonomous: boolean;
  changed: boolean;
  message: string;
}

export interface RunAgentResponse {
  status: string;
  task_id: string;
  portfolio_id: number;
}

/* ------------------------------------------------------------------ */
/* WebSocket contract                                                  */
/* ------------------------------------------------------------------ */

export type AgentStage =
  | 'analyst_started'
  | 'analyst_finished'
  | 'cio_started'
  | 'cio_finished';

export interface AgentThinkingEvent {
  ticker: string;
  stage: AgentStage;
  message: string;
  portfolio_id: number;
  at: string;
}

export interface PortfolioSnapshotEvent {
  metrics: PortfolioMetrics;
  assets: Asset[];
  captured_at: string;
}

export interface AutonomyChangedEvent {
  is_autonomous: boolean;
}

/**
 * Greeting frame the consumer pushes right after it accepts the socket. It
 * carries no portfolio state — it only confirms the stream is live.
 */
export interface ConnectionEstablishedEvent {
  username: string;
  group: string;
}

/** Discriminated union of every frame the backend may push. */
export type StreamMessage =
  | { type: 'connection.established'; payload: ConnectionEstablishedEvent }
  | { type: 'portfolio.snapshot'; payload: PortfolioSnapshotEvent }
  | { type: 'trade.executed'; payload: Transaction }
  | { type: 'decision.created'; payload: DecisionLog }
  | { type: 'agent.thinking'; payload: AgentThinkingEvent }
  | { type: 'recommendation.created'; payload: Recommendation }
  | { type: 'recommendation.updated'; payload: Recommendation }
  | { type: 'autonomy.changed'; payload: AutonomyChangedEvent };

export type StreamMessageType = StreamMessage['type'];

/** Socket lifecycle as rendered by the header indicator. */
export type ConnectionStatus = 'idle' | 'connecting' | 'live' | 'reconnecting' | 'offline';

/* ------------------------------------------------------------------ */
/* UI helpers                                                          */
/* ------------------------------------------------------------------ */

export interface AsyncState {
  loading: boolean;
  error: string | null;
}

export interface Toast {
  id: number;
  kind: 'success' | 'error' | 'info';
  message: string;
}
