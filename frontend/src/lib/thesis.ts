import type { DecisionLog, MarketSentiment } from '../types';

/**
 * The portfolio's current investment thesis.
 *
 * TODO(current-thesis): **not wired to anything, because no endpoint exposes a
 * thesis.** `GET /api/portfolio/logs/` returns one row per agent invocation
 * (action, sentiment, ticker, reasoning) — that is an audit trail of decisions,
 * not a standing thesis, and reading the newest row as "the thesis" would
 * invent a claim the backend never made. In particular a log row says nothing
 * about which positions the thesis still covers, or whether it has been
 * superseded.
 *
 * When the backend exposes one (e.g. `GET /api/portfolio/thesis/`), parse it
 * into this shape and render it; until then `currentThesisFromLogs` returns
 * `null` so callers must treat the thesis as unavailable rather than render a
 * placeholder that looks like data.
 */
export interface CurrentThesis {
  /** Instrument the thesis is about, or `null` for a whole-portfolio view. */
  ticker: string | null;
  /** Proposed or taken action, e.g. `"BUY"` / `"HOLD"`. */
  action: string;
  sentiment: MarketSentiment | null;
  /** Why — the CIO's reasoning, verbatim. */
  rationale: string | null;
  /** Decision log the thesis was derived from, once the backend keys it. */
  decisionId: number | null;
  /** When the thesis was last asserted, ISO-8601. */
  statedAt: string;
}

/**
 * Derive the current thesis from decision logs.
 *
 * Deliberately returns `null`: the logs carry per-decision reasoning, and
 * presenting the latest row as a *current thesis* would fabricate a payload the
 * API does not serve. Kept as a typed seam so the panel can be added in one
 * place once the endpoint exists.
 */
export function currentThesisFromLogs(_logs: DecisionLog[] | null | undefined): CurrentThesis | null {
  return null;
}
