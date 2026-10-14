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

/**
 * Today's AI spend against the daily ceiling, as returned by the API and pushed
 * on every `budget.updated`.
 *
 * All three money values are decimal strings, like every other amount on the
 * wire, so they are parsed before formatting rather than coerced.
 */
export interface AiBudget {
  spent_usd: Decimal;
  limit_usd: Decimal;
  remaining_usd: Decimal;
  used_pct: number;
  /** False when the ceiling is tracked but not enforced. */
  enforced: boolean;
  /** True when runs are halted until the UTC day rolls over. */
  exhausted: boolean;
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
  /**
   * Ceiling on total invested capital, as a percentage of equity.
   *
   * Optional because the deployed serializer did not always publish it: the
   * column exists on the model, but a payload without it must render as "not
   * published" rather than as a 0% ceiling (which would read as a total ban on
   * holding anything).
   */
  max_portfolio_exposure_pct?: Decimal;
  /** Ceiling on any one instrument, as a percentage of equity. Same caveat. */
  max_ticker_concentration_pct?: Decimal;
  created_at: string;
  metrics: PortfolioMetrics;
  assets: Asset[];
  ai_budget: AiBudget;
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
  /**
   * Billed split behind `tokens_used`, so the stored cost reads as arithmetic
   * rather than a figure taken on trust. `input_tokens` is the full prompt count
   * and already includes the cached and cache-write portions, billed at 0.1x and
   * 1.25x of the input rate — ignoring that overstated a real run by 93%.
   */
  input_tokens: number | null;
  output_tokens: number | null;
  cached_input_tokens: number | null;
  cache_write_tokens: number | null;
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
/* Decision attribution ("was the agent right?")                       */
/* ------------------------------------------------------------------ */

/**
 * Aggregate decision quality at one horizon, from
 * `GET /api/portfolio/attribution/`.
 *
 * The nulls are load-bearing and must not be flattened to zero:
 *
 * * `hit_rate_pct` is `null` — not `0` — when nothing directional has been
 *   scored yet. "Not measured" and "measured as total failure" are different
 *   claims, and the backend deliberately distinguishes them.
 * * `avg_benchmark_return_pct` / `avg_alpha_pct` are `null` when no benchmark
 *   price was available for any scored decision. An unknown comparison is not a
 *   loss.
 */
/**
 * One row of `GET /api/portfolio/thesis/` — why a position is held, and what
 * would prove that wrong.
 *
 * `opened_by_decision` is nullable because the decision that opened the position
 * may since have been pruned from the audit trail. `invalidation` is a string
 * that may be empty: an agent that cannot name a falsifier has said something
 * useful, and the UI states it rather than printing a blank.
 */
export interface ThesisRow {
  id: number;
  ticker: string;
  thesis: string;
  invalidation: string;
  status: 'OPEN' | 'CLOSED' | 'INVALIDATED';
  opened_at: string;
  updated_at: string;
  closed_at: string | null;
  close_reason: string;
  opened_by_decision: number | null;
}

export interface AttributionScorecard {
  horizon_days: number;
  /** Decisions whose horizon has elapsed and which now have an outcome row. */
  evaluated: number;
  /** Of those, the ones that made a directional claim (BUY / SELL). */
  directional_calls: number;
  hits: number;
  /** `null` when `directional_calls` is 0 — render as `—`, never `0%`. */
  hit_rate_pct: number | null;
  /** Decimal string; `"0.0000"` when nothing has been scored at this horizon. */
  avg_decision_return_pct: Decimal | null;
  avg_benchmark_return_pct: Decimal | null;
  /** Average decision return minus average benchmark return; `null` without a benchmark. */
  avg_alpha_pct: Decimal | null;
  beat_benchmark: number;
  benchmarked: number;
}

/** One scored decision at one horizon — a row of evidence behind the scorecard. */
export interface DecisionOutcomeRow {
  id: number;
  decision_id: number;
  horizon_days: number;
  ticker: string;
  action: string;
  price_at_decision: Decimal;
  price_at_horizon: Decimal;
  forward_return_pct: Decimal;
  /** What the action was worth (a SELL profits from a fall). */
  decision_return_pct: Decimal;
  benchmark_return_pct: Decimal | null;
  /**
   * `true` = right, `false` = wrong, `null` = the decision made no directional
   * claim (a HOLD) and is therefore neither. Never render `null` as a miss.
   */
  directional_hit: boolean | null;
  evaluated_at: string;
}

/** `GET /api/portfolio/attribution/` — the scorecard plus the rows behind it. */
export interface AttributionResponse {
  /** Benchmark symbol the alpha is measured against, e.g. `"SPY"`. */
  benchmark: string;
  horizons: AttributionScorecard[];
  recent: DecisionOutcomeRow[];
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
  | { type: 'autonomy.changed'; payload: AutonomyChangedEvent }
  | { type: 'budget.updated'; payload: AiBudget }
  | { type: 'budget.exhausted'; payload: AiBudget };

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
