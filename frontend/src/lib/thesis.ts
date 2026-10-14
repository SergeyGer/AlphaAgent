import type { ThesisRow } from '../types';

/**
 * The live investment thesis for a position.
 *
 * Backed by `GET /api/portfolio/thesis/`, which returns one row per open
 * position — the reason it is held and the condition that would falsify it. The
 * agents receive the same text on every run, so what the dashboard shows here is
 * exactly what the CIO is arguing against when it proposes an exit.
 *
 * This started as a typed seam returning `null`, because at the time no endpoint
 * exposed a thesis and reading the newest decision log as "the thesis" would have
 * invented a claim the backend never made. The endpoint now exists, so the seam
 * is filled in rather than deleted — `thesisFromRow` is the single place that
 * knows the wire shape.
 */
export interface CurrentThesis {
  /** Instrument the thesis is about. One thesis is always about one instrument. */
  ticker: string;
  /** Why the position is worth holding, in the agent's words. */
  rationale: string;
  /**
   * The observable condition that would make the thesis wrong.
   *
   * Deliberately nullable in effect (`""` means the agent named none): a thesis
   * without a falsifier is a weaker claim, and the UI should be able to say so
   * rather than print an empty line that looks like a rendering bug.
   */
  invalidation: string;
  /** Decision log that opened the position this thesis describes. */
  decisionId: number | null;
  /** When the thesis was recorded, ISO-8601. */
  statedAt: string;
}

/** Map one row of the thesis endpoint onto the view model. */
export function thesisFromRow(row: ThesisRow): CurrentThesis {
  return {
    ticker: row.ticker,
    rationale: row.thesis,
    invalidation: (row.invalidation ?? '').trim(),
    decisionId: row.opened_by_decision ?? null,
    statedAt: row.opened_at,
  };
}

/**
 * The thesis for one instrument, or `null` when none is recorded.
 *
 * Returns null rather than a placeholder for an instrument with no thesis.
 * "No thesis recorded" is a real and useful state — it means the position was
 * opened before theses existed, or by an operator-approved trade, and the agent
 * is told the same thing — so it must not be rendered as an empty thesis.
 */
export function thesisFor(
  rows: ThesisRow[] | null | undefined,
  ticker: string | null | undefined,
): CurrentThesis | null {
  if (!rows?.length || !ticker) return null;
  const wanted = ticker.trim().toUpperCase();
  const row = rows.find((candidate) => (candidate.ticker ?? '').toUpperCase() === wanted);
  return row ? thesisFromRow(row) : null;
}
