import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { PositionTheses } from './PositionTheses';
import type { ThesisRow } from '../types';

/**
 * The position theses panel.
 *
 * The state worth testing is the *absent* one. A position with no recorded
 * thesis is legitimate — opened before theses existed, or by an operator-approved
 * trade — and the panel must render nothing rather than an empty shell that reads
 * as a broken feature. A thesis with no invalidation must likewise say so, since
 * an agent that cannot name a falsifier has told you something.
 */

const row = (over: Partial<ThesisRow> = {}): ThesisRow => ({
  id: 1,
  ticker: 'AAPL',
  thesis: 'Services margin expanding and the upgrade cycle is intact.',
  invalidation: 'Gross margin below 40% for two consecutive quarters.',
  status: 'OPEN',
  opened_at: '2026-10-14T13:00:00Z',
  updated_at: '2026-10-14T13:00:00Z',
  closed_at: null,
  close_reason: '',
  opened_by_decision: 1490,
  ...over,
});

describe('the paginated envelope', () => {
  /*
   * `GET /api/portfolio/thesis/` is a ListAPIView under DRF's default pagination,
   * so it returns `{count, next, previous, results}`. The client originally typed
   * it as a bare array, which made `theses.data.length` undefined and the panel
   * render nothing — no error, no console warning, just an absent section. The
   * unwrapping is asserted here because nothing else would catch its removal.
   */
  it('is unwrapped by the API client rather than leaking into the panel', async () => {
    const { api } = await import('../api/client');
    const payload = { count: 1, next: null, previous: null, results: [row()] };
    const original = globalThis.fetch;

    globalThis.fetch = (async () =>
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })) as typeof fetch;

    try {
      const rows = await api.thesis();
      expect(Array.isArray(rows)).toBe(true);
      expect(rows).toHaveLength(1);
      expect(rows[0]?.ticker).toBe('AAPL');
    } finally {
      globalThis.fetch = original;
    }
  });
});

describe('PositionTheses', () => {
  it('renders the thesis and its invalidation condition', () => {
    render(<PositionTheses theses={[row()]} loading={false} />);

    expect(screen.getByText('AAPL')).toBeInTheDocument();
    expect(screen.getByText(/Services margin expanding/)).toBeInTheDocument();
    expect(screen.getByText(/Gross margin below 40%/)).toBeInTheDocument();
  });

  it('states when no invalidation was recorded rather than leaving a gap', () => {
    render(<PositionTheses theses={[row({ invalidation: '   ' })]} loading={false} />);

    expect(screen.getByText(/no invalidation condition recorded/i)).toBeInTheDocument();
  });

  it('renders nothing when there are no theses on record', () => {
    // Not an error state: a position can legitimately have none.
    const { container } = render(<PositionTheses theses={[]} loading={false} />);

    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing for a null payload', () => {
    const { container } = render(<PositionTheses theses={null} loading={false} />);

    expect(container).toBeEmptyDOMElement();
  });

  it('shows a loading placeholder before the first payload arrives', () => {
    const { container } = render(<PositionTheses theses={null} loading />);

    expect(container).not.toBeEmptyDOMElement();
    expect(screen.queryByText(/Position theses/)).not.toBeInTheDocument();
  });

  it('lists several positions with the count in the heading', () => {
    render(
      <PositionTheses
        theses={[row(), row({ id: 2, ticker: 'TSLA', thesis: 'Deliveries recovering.' })]}
        loading={false}
      />,
    );

    expect(screen.getByText(/2 open/)).toBeInTheDocument();
    expect(screen.getByText('TSLA')).toBeInTheDocument();
  });

  it('survives a hostile payload without rendering undefined', () => {
    const broken = [
      row({ thesis: undefined as never, invalidation: undefined as never, ticker: 'BTC' }),
    ];

    const { container } = render(<PositionTheses theses={broken} loading={false} />);

    expect(container.textContent).not.toMatch(/undefined|NaN/);
  });
});
