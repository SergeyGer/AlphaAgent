import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { EvidenceDrawer } from './EvidenceDrawer';
import { evidence, missingTimestampEvidence } from '../test/decisionRunFixtures';

/**
 * UX-02 — the evidence drawer.
 *
 * The two hard requirements are negative in form: a null publication time must
 * not look like a date, and opening the drawer must not cause a fetch.
 */

afterEach(() => {
  vi.restoreAllMocks();
});

function renderDrawer(
  items = [evidence(), missingTimestampEvidence()],
  overrides: { initialEvidenceId?: string | null } = {},
): ReturnType<typeof render> {
  return render(
    <EvidenceDrawer
      open
      evidence={items}
      initialEvidenceId={overrides.initialEvidenceId ?? 'ev-2'}
      onClose={vi.fn()}
    />,
  );
}

describe('EvidenceDrawer', () => {
  it('renders a null published_at as unknown and never as a date', () => {
    render(
      <EvidenceDrawer
        open
        evidence={[missingTimestampEvidence()]}
        onClose={vi.fn()}
      />,
    );

    const published = screen.getByTestId('evidence-published-value');
    expect(published).toHaveTextContent('Unknown');
    expect(published).toHaveTextContent(/did not publish a timestamp/i);
    // No year, no epoch, no date-like separator can have leaked in.
    expect(published.textContent ?? '').not.toMatch(/\b(19|20)\d{2}\b/);
    expect(published.textContent ?? '').not.toMatch(/\d{1,2}[/-]\d{1,2}[/-]\d{2,4}/);
    expect(published.textContent ?? '').not.toMatch(/1970/);
  });

  it('renders publisher, source link, retrieval time, excerpt and observation', () => {
    const onClose = vi.fn();
    render(
      <EvidenceDrawer open evidence={[evidence()]} onClose={onClose} />,
    );

    expect(screen.getAllByText('Example Wire').length).toBeGreaterThanOrEqual(1);
    const link = screen.getByRole('link', { name: /example\.com\/nvda-10q/ });
    expect(link).toHaveAttribute('target', '_blank');
    expect(link.getAttribute('rel')).toContain('noopener');
    expect(link.getAttribute('rel')).toContain('noreferrer');

    expect(screen.getByText(/Data-centre revenue rose 94%/)).toBeInTheDocument();
    expect(screen.getByText('94')).toBeInTheDocument();
    expect(screen.getByText('percent YoY')).toBeInTheDocument();
    expect(screen.getAllByText(/UTC/).length).toBeGreaterThanOrEqual(2);
  });

  it('flags missing publisher, excerpt, observation and source link', () => {
    render(
      <EvidenceDrawer
        open
        evidence={[missingTimestampEvidence()]}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getAllByText('Publisher unknown').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('No excerpt stored for this source.')).toBeInTheDocument();
    expect(screen.getByText('No typed observation stored for this source.')).toBeInTheDocument();
    expect(screen.getAllByText('No source link stored.')).toHaveLength(2);
    expect(screen.getByRole('region', { name: 'Data quality flags' })).toHaveTextContent(
      'Publication time unknown',
    );
  });

  it('does not render a non-http source URI as a link', () => {
    render(
      <EvidenceDrawer
        open
        evidence={[missingTimestampEvidence({ source_uri: 'javascript:alert(1)' })]}
        onClose={vi.fn()}
      />,
    );

    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.getByText(/not an http\(s\) link/)).toBeInTheDocument();
  });

  it('focuses the close button on open and closes on Escape', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<EvidenceDrawer open evidence={[evidence()]} onClose={onClose} />);

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Close evidence drawer' })).toHaveFocus(),
    );
    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('shows an honest empty state when no evidence was persisted', () => {
    render(<EvidenceDrawer open evidence={[]} onClose={vi.fn()} />);

    expect(screen.getByText('No evidence stored')).toBeInTheDocument();
    expect(
      screen.getByText(/Nothing is reconstructed in its place/),
    ).toBeInTheDocument();
    expect(screen.queryByTestId('evidence-published-value')).toBeNull();
  });

  it('selects the linked item when opened from a claim', () => {
    renderDrawer([evidence(), missingTimestampEvidence()], { initialEvidenceId: 'ev-1' });

    expect(within(screen.getByTestId('evidence-published-value')).getByText(/Nov 2026/)).toBeInTheDocument();
  });
});
