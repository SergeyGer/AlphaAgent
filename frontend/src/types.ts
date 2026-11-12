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
  /** Billed spend only. A null provider charge is not added here. */
  spent_usd: Decimal;
  limit_usd: Decimal;
  /** Remaining against the conservative committed figure, not billed alone. */
  remaining_usd: Decimal;
  used_pct: number;
  /** False when the ceiling is tracked but not enforced. */
  enforced: boolean;
  /** True when runs are halted until the UTC day rolls over. */
  exhausted: boolean;
  /** Price-table estimate for calls whose provider reported no dollars. */
  estimated_spend_usd?: Decimal;
  /** Upper bounds held for calls that have not settled (including unknown). */
  reserved_usd?: Decimal;
  /** The part of reserved_usd held against unknown-cost calls. */
  unknown_reserved_usd?: Decimal;
  /** billed + estimated + reserved: the figure the ceiling is enforced on. */
  committed_usd?: Decimal;
  /** Count of runs whose charge was never reported; no dollar value is implied. */
  unknown_cost_runs?: number;
  /** Always null when published: an unreported charge cannot be quantified. */
  unknown_amount_usd?: Decimal | null;
  /** True when spent/committed is a lower bound rather than a complete total. */
  lower_bound?: boolean;
  basis?: string;
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
  /**
   * The decision run that caused this execution, when the API publishes the
   * link. Optional on purpose: Phase 0 rows predate decision runs and no
   * mapping can be invented for them.
   */
  run_id?: string | number | null;
  decision_run_id?: string | number | null;
  run?: { id?: string | number | null } | null;
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
  /**
   * The structured decision run that explains this legacy audit row, when one
   * exists. `DecisionRun.legacy_log` is the join; the API may publish it as
   * `run_id`, `decision_run_id` or a nested `run.id`. All three are accepted so
   * the link appears whichever shape the producer settles on.
   */
  run_id?: string | number | null;
  decision_run_id?: string | number | null;
  run?: { id?: string | number | null } | null;
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
  /** Associated decision run, when the log row carries a mapping. */
  runId?: string | null;
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
  /** Associated decision run, when the scorecard row publishes the mapping. */
  run_id?: string | number | null;
  decision_run_id?: string | number | null;
  run?: { id?: string | number | null } | null;
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
/* Decision runs — Phase 1 explainability contract                     */
/* ------------------------------------------------------------------ */

/**
 * How a run was produced. This is a provenance label, not a quality signal:
 * `heuristic` and `legacy` specifically mean the LLM debate did not happen, and
 * the detail view has to say so rather than presenting their data as complete.
 */
export type RunMode = 'live' | 'recorded' | 'heuristic' | 'legacy' | 'synthetic';

/** Pipeline stage / terminal state of one decision run. */
export type RunStatus =
  | 'queued'
  | 'collecting'
  | 'analysing'
  | 'rebutting'
  | 'adjudicating'
  | 'validating'
  | 'awaiting_approval'
  | 'completed'
  | 'failed'
  | 'cancelled';

/** Header row from `GET /api/decision-runs/`. */
export interface RunSummary {
  id: string;
  ticker: string;
  status: RunStatus;
  mode: RunMode;
  protocol_version: string;
  started_at: string | null;
  completed_at: string | null;
  latency_ms: number | null;
  error_code: string | null;
  /**
   * Provenance fields published by the detail/metadata serializer. Optional
   * because the list endpoint and older deployments do not always include the
   * richer metadata envelope.
   */
  is_synthetic?: boolean;
  source?: string;
  legacy_log_id?: number | string | null;
  fixture_id?: string | null;
  /** Present on the detail/metadata payload; null when the cursor hides it. */
  execution_state?: string | null;
  execution?: {
    state: string | null;
    executed: boolean;
    awaiting_approval: boolean;
    no_trade: boolean;
    attempt?: Record<string, unknown> | null;
  } | null;
  /**
   * The export endpoint publishes the full metadata envelope. These are kept
   * on the imported run unchanged rather than being re-declared field by
   * field: the export is the shape, and the viewer must not maintain a second,
   * drifted copy of it.
   */
  status_label?: string | null;
  cost?: Record<string, unknown> | null;
  failure?: Record<string, unknown> | null;
  provenance?: Record<string, unknown> | null;
  protocol?: Record<string, unknown> | null;
  snapshot_id?: string | null;
  model_hash?: string | null;
  config_hash?: string | null;
  prompt_hash?: string | null;
  created_at?: string | null;
  stage_states?: Record<string, unknown> | null;
  error_detail?: string | null;
}

export type AgentRole = 'bull' | 'bear';
export type ClaimType = 'observed_fact' | 'interpretation' | 'forecast';
export type RebuttalResponseType = 'agree' | 'dispute' | 'qualify' | 'insufficient_evidence';

/** One factual/interpretive/forecast claim made by a debating agent. */
export interface Claim {
  claim_id: string;
  agent_role: AgentRole;
  stance: string;
  /** Maximum 280 characters on the wire. */
  statement: string;
  claim_type: ClaimType;
  evidence_ids: string[];
  horizon: string | null;
  limitations: string | null;
}

/** A response aimed at one specific claim (Phase 2 populates these). */
export interface Rebuttal {
  rebuttal_id: string;
  responding_agent: AgentRole;
  target_claim_id: string;
  response_type: RebuttalResponseType;
  explanation: string;
  evidence_ids: string[];
  position_change: string | null;
}

/** A typed numeric observation attached to evidence. */
export interface EvidenceObservation {
  value: string;
  unit: string;
  currency: string | null;
}

/** One stored source behind a claim or rebuttal. */
export interface EvidenceItem {
  id: string;
  source_type: string;
  source_uri: string | null;
  publisher: string | null;
  /** `null` means the source did not publish a timestamp — not "at zero". */
  published_at: string | null;
  retrieved_at: string;
  excerpt: string | null;
  observation: EvidenceObservation | null;
  content_hash: string;
}

export type GuardCheckOutcome = 'pass' | 'fail' | 'not_evaluated';

export interface GuardCheck {
  rule_code: string;
  outcome: GuardCheckOutcome;
  threshold: string | null;
  actual: string | null;
  unit: string | null;
}

/**
 * The deterministic risk guard's comparison of a requested vs allowed order.
 *
 * `id` is the stable identifier of one evaluation. A run can contain several
 * (a preflight check and a final check, for example); replay resolves the one
 * visible at its cursor rather than always showing the summary. A legacy guard
 * event with no resolvable identifier produces a partial object: the fields the
 * event recorded are shown and `unavailable_fields` names the rest.
 */
export interface GuardComparison {
  id?: string | null;
  status: string;
  requested_quantity: string | null;
  allowed_quantity: string | null;
  binding_rule: string | null;
  reason_codes: string[];
  checks: GuardCheck[];
  quote_time?: string | null;
  quote_source?: string | null;
  quote_retrieved_at?: string | null;
  policy_version?: string | null;
  created_at?: string | null;
  /** True when only the event payload could be resolved, not the full record. */
  partial?: boolean;
  /** Field names the legacy event did not record. Rendered as "Unavailable". */
  unavailable_fields?: string[];
}

export type OutcomeMaturity = 'pending' | 'matured' | 'evaluated' | 'unavailable';

/** One horizon's measured (or explicitly unmeasured) outcome. */
export interface OutcomeView {
  id?: string;
  horizon_days: number;
  maturity: OutcomeMaturity;
  signal_score: string | null;
  benchmark: string | null;
  /** Often null in Phase 1 — that is rendered as "Not available", never zero. */
  portfolio_impact: string | null;
  method_version: string;
}

/** One horizon's scheduler state, used to decide whether to keep polling. */
export interface OutcomeScheduleEntry {
  horizon_days: number;
  state: OutcomeMaturity | string;
  due_at: string | null;
  reason: string | null;
  evaluation_id: string | null;
  measurement_basis: string | null;
}

export interface OutcomeSchedule {
  method_version: string;
  entries: OutcomeScheduleEntry[];
  /** True while a later horizon is pending, matured or awaiting evaluation. */
  pending: boolean;
  next_due_at: string | null;
}

/**
 * The full detail payload from `GET /api/decision-runs/{id}/detail/`.
 *
 * Named `DecisionDetailPayload` in the frontend because `DecisionDetail` is
 * already the primary screen component.
 */
export interface DecisionDetailPayload {
  run: RunSummary;
  /**
   * The decided action, when a producer supplies it outside the rationale.
   * Absent from the Phase 1 contract's required fields, so it is optional and
   * rendered only if present — never inferred from a claim.
   */
  action?: string | null;
  rationale: string | null;
  decisive_claim_ids: string[];
  strongest_objection: string | null;
  claims: Claim[];
  rebuttals: Rebuttal[];
  evidence: EvidenceItem[];
  /** Summary: the most recent guard evaluation (for a finished view). */
  guard: GuardComparison | null;
  /** Every guard evaluation, each with a stable identifier. */
  guards?: GuardComparison[];
  /** Explicit alias for the same ordered collection (newer producers). */
  guard_evaluations?: GuardComparison[];
  outcomes: OutcomeView[];
  /** Superseded outcome revisions, kept visible rather than double-counted. */
  superseded_outcomes?: OutcomeView[];
  /** Pending measurement work, used to keep a completed detail view polling. */
  outcome_schedule?: OutcomeSchedule | null;
  /** False for legacy/sequential runs that never had a rebuttal stage. */
  has_rebuttals: boolean;
  /*
   * Sections the authenticated detail endpoint publishes and the export
   * carries verbatim. They may be absent from an older capture, so every one
   * is optional; when present the browser keeps the producer's object rather
   * than projecting it into a narrower local type.
   */
  verdict?: Record<string, unknown> | null;
  snapshot?: Record<string, unknown> | null;
  limitations?: Array<{ source?: string; text?: string }>;
  redaction?: Record<string, unknown> | null;
}

/** One persisted pipeline event, ordered by `seq`. */
export interface RunEvent {
  event_id: string;
  seq: number;
  type: string;
  stage: string | null;
  agent_role: string | null;
  occurred_at: string;
  payload: Record<string, unknown>;
  /** Present on the export endpoint; absent from the event-window response. */
  schema_version?: string;
  persisted_at?: string | null;
}

/** `GET /api/decision-runs/` envelope. */
export interface RunListResponse {
  results: RunSummary[];
}

/** `GET /api/decision-runs/{id}/events/?after_seq=N` envelope. */
export interface RunEventsResponse {
  events: RunEvent[];
  /** URL of the next window, or null when this page reached the server tail. */
  next: string | null;
  /**
   * Last sequence *in this window*. It is not the server tail: a page can stop
   * at 100 while the run has 340 persisted events. Kept required for older
   * producers; `latest_sequence` is the authoritative tail.
   */
  last_sequence: number;
  /** Newest persisted sequence for the run (the server tail). */
  latest_sequence?: number;
  /** Echo of the requested cursor and page size, for diagnostics. */
  after_seq?: number;
  limit?: number;
  count?: number;
  run_id?: string;
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

/* ------------------------------------------------------------------ */
/* Advisory portfolio plans (FIX-08)                                   */
/* ------------------------------------------------------------------ */

export type PlanStatus =
  | 'proposed'
  | 'applied'
  | 'no_change'
  | 'failed'
  | 'superseded'
  | 'cancelled'
  | 'rejected';

export type PlanApprovalState =
  | 'pending'
  | 'approved'
  | 'expired'
  | 'invalidated'
  | 'rejected'
  | 'cancelled'
  | 'consumed'
  | 'not_required';

export interface PlanWeightsRow {
  ticker: string;
  is_cash: boolean;
  current_weight: Decimal | null;
  requested_weight: Decimal | null;
  constrained_weight: Decimal | null;
  change_from_current: Decimal | null;
  requested_vs_constrained: Decimal | null;
  executed_weight: Decimal | null;
  action: string | null;
  rationale: string | null;
  claim_ids: string[];
}

export interface PlanSectorEffectRow {
  sector: string;
  current_weight: Decimal | null;
  target_weight: Decimal | null;
  change: Decimal | null;
}

export interface PlanHolding {
  ticker: string;
  quantity: Decimal;
  avg_cost: Decimal;
  price: Decimal | null;
  price_source?: string | null;
  market_value_usd: Decimal | null;
  weight: Decimal | null;
  sector: string | null;
  asset_class: string | null;
  classification_source: string | null;
}

export interface PlanLegView {
  leg_id: number;
  sequence: number;
  ticker: string;
  side: 'BUY' | 'SELL' | string;
  quantity: Decimal;
  price: Decimal;
  notional_usd: Decimal;
  estimated_cost_usd: Decimal;
  priority: string;
  sector: string | null;
  asset_class: string | null;
  status: string;
  transaction_id: number | null;
  applied_at: string | null;
  reason: string;
  signed_cash_effect_usd: Decimal;
}

export interface PlanApprovalView {
  state: PlanApprovalState;
  executable: boolean;
  required: boolean;
  plan_version: number;
  approved_version: number | null;
  approval_hash: string | null;
  approved_at: string | null;
  approved_by: string | null;
  approved_by_id: number | null;
  expires_at: string | null;
  seconds_remaining: number | null;
  decided_at: string | null;
  note: string;
  reason: string;
}

export interface PlanApprovalHistoryRow {
  approval_id: number;
  decision: 'approved' | 'rejected' | 'cancelled' | string;
  plan_version: number;
  approval_hash: string | null;
  decided_at: string | null;
  decided_by: string | null;
  expires_at: string | null;
  note: string;
}

export interface PlanProvenanceView {
  provider: string | null;
  model: string | null;
  mode: string;
  synthetic_response: boolean;
  label: string;
  prompt_template_version: string | null;
  protocol_version: string | null;
  prompt_hash: string | null;
  parameters: Record<string, unknown>;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: Decimal | null;
  cost_reported: boolean | null;
  token_accounting: string | null;
  adapter: Record<string, unknown> | null;
  calls: Array<Record<string, unknown>>;
  note: string | null;
}

export interface PlanCashEffect {
  current_cash_weight: Decimal | null;
  requested_cash_weight: Decimal | null;
  constrained_cash_weight: Decimal | null;
  current_cash_usd: Decimal | null;
  target_cash_usd: Decimal | null;
  min_cash_usd: Decimal | null;
  change_usd: Decimal | null;
  buffer_status: 'within_policy' | 'breach' | 'unknown' | string;
  note: string;
}

export interface PlanSectorEffects {
  rows: PlanSectorEffectRow[];
  asset_class_target: Record<string, Decimal>;
  unclassified_target_weight: Decimal | null;
  unclassified_target_tickers: string[];
  current_coverage: Record<string, unknown>;
  target_coverage_status: 'complete' | 'incomplete' | string;
  note: string;
}

export interface PlanDataQuality {
  flags?: Array<{ code: string; severity: string; detail: string }>;
  limitations?: string[];
  declared_policies?: Array<Record<string, unknown>>;
  risk_statistics?: {
    status: string | null;
    method_version: string | null;
    history_source: string | null;
    observation_window: Record<string, string> | null;
    min_observations: number | null;
    missing_pairs: Array<Record<string, unknown>>;
    quality_flags: string[];
    annualisation: string | null;
    note: string | null;
  };
  sector_coverage?: Record<string, unknown>;
  source?: Record<string, string>;
  [key: string]: unknown;
}

export interface PlanReview {
  plan_id: string;
  portfolio_id: number;
  status: PlanStatus;
  reviewable: boolean;
  plan_version: number;
  review_fingerprint: string;
  created_at: string | null;
  updated_at: string | null;
  applied_at: string | null;
  execution_mode: string;
  protocol_version: string;
  policy_version: string;
  proposal_portfolio_version: string;
  evaluated_portfolio_version: string;
  brief_hash: string | null;
  approval_hash: string | null;
  data_sufficiency: string | null;
  objective: string;
  rationale: string;
  actions: Array<Record<string, unknown>>;
  holdings: PlanHolding[];
  weights: PlanWeightsRow[];
  requested_target_weights: Record<string, Decimal>;
  constrained_target_weights: Record<string, Decimal>;
  cash_effect: PlanCashEffect;
  sector_effects: PlanSectorEffects;
  funding: Record<string, unknown>;
  costs: {
    estimated_cost_usd: Decimal;
    estimated_turnover_pct: Decimal | null;
    cost_model: Record<string, unknown>;
    per_leg_estimated_cost_usd: Record<string, Decimal>;
    recorded_fee_note: string;
    fees_recorded_in_ledger: boolean;
  };
  estimated_turnover_pct: Decimal | null;
  rejected_alternatives: Array<{ ticker: string | null; reason: string }>;
  unresolved_uncertainties: string[];
  deviations: Array<{ code: string; detail?: string; [key: string]: unknown }>;
  constraints: Record<string, unknown>;
  data_quality: PlanDataQuality;
  limits: Record<string, unknown>;
  legs: PlanLegView[];
  provenance: PlanProvenanceView;
  submission: {
    run_id: string;
    mode: string;
    status: string;
    synthetic_response: boolean;
    provider: string | null;
    model: string | null;
    request_hash: string;
  } | null;
  approval: PlanApprovalView;
  approval_history: PlanApprovalHistoryRow[];
  execution: {
    status: string;
    applied_at: string | null;
    applied_by: string | null;
    applied_approval_version: number | null;
    executed_target_weights: Record<string, Decimal>;
    reasons: Array<Record<string, unknown>>;
    transaction_ids: number[];
    failure_code: string | null;
    failure_detail: string | null;
  };
  revalidation: {
    required: boolean;
    count: number;
    revalidated_at: string | null;
  };
  review_actions: {
    can_approve: boolean;
    can_reject: boolean;
    can_cancel: boolean;
    can_apply: boolean;
    can_revalidate: boolean;
    default_apply_mode: string;
  };
  immutability: {
    plan_version: number;
    approval_version: number | null;
    approval_required: boolean;
    requires_expiry: boolean;
    note: string;
  };
}

export interface PlanSummary {
  plan_id: string;
  portfolio_id: number;
  status: PlanStatus;
  plan_version: number;
  protocol_version: string;
  provider: string | null;
  model: string | null;
  synthetic_response: boolean;
  approval_state: PlanApprovalState;
  approval_expires_at: string | null;
  objective: string;
  requested_weights: Record<string, Decimal>;
  constrained_weights: Record<string, Decimal>;
  estimated_cost_usd: Decimal;
  data_sufficiency: string | null;
  revalidation_required: boolean;
  created_at: string | null;
  applied_at: string | null;
}

export interface PlanListResponse {
  count: number;
  results: PlanSummary[];
}

export interface PlanDecisionResponse {
  approval: PlanApprovalHistoryRow;
  review: PlanReview;
}

export interface PlanApplyResponse {
  application: {
    plan_id: string;
    status: PlanStatus;
    already_applied: boolean;
    mode: string;
    transaction_ids: number[];
    legs: Array<Record<string, unknown>>;
  };
  input: Record<string, unknown>;
  review: PlanReview;
}
