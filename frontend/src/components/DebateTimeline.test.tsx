import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { DebateTimeline } from './DebateTimeline';
import { TIMELINE_EVENTS, runEvent } from '../test/decisionRunFixtures';

/**
 * Stage-level timeline. The event stream contains token-shaped events; the
 * timeline must collapse them, and it must not render a rebuttal stage for a
 * protocol that never had one.
 */

describe('DebateTimeline', () => {
  it('renders one node per stage and collapses token-level events', () => {
    render(<DebateTimeline events={TIMELINE_EVENTS} hasRebuttals />);

    const list = screen.getByRole('list', { name: 'Decision timeline' });
    const nodes = within(list).getAllByRole('listitem');
    expect(nodes).toHaveLength(6);

    const collecting = within(list).getByText('Collecting evidence').closest('li');
    expect(collecting).not.toBeNull();
    expect(within(collecting as HTMLElement).getByText('3 stage events')).toBeInTheDocument();
    // Token text is not rendered as its own node.
    expect(within(list).queryByText('Data-centre')).toBeNull();
    expect(screen.getByText('6 stages')).toBeInTheDocument();
  });

  it('renders no rebuttal node when has_rebuttals is false', () => {
    render(<DebateTimeline events={TIMELINE_EVENTS} hasRebuttals={false} />);

    expect(screen.queryByText('Rebutting')).toBeNull();
    const nodes = within(screen.getByRole('list', { name: 'Decision timeline' })).getAllByRole(
      'listitem',
    );
    expect(nodes).toHaveLength(5);
  });

  it('renders no rebuttal placeholder when every event is a rebuttal and the protocol has none', () => {
    const onlyRebuttal = [
      runEvent({ event_id: 'r1', seq: 1, stage: 'rebutting', type: 'rebuttal.created' }),
    ];
    render(<DebateTimeline events={onlyRebuttal} hasRebuttals={false} />);

    expect(screen.queryByText('Rebutting')).toBeNull();
    expect(screen.queryByRole('list', { name: 'Decision timeline' })).toBeNull();
    expect(screen.getByText(/No stage events were recorded/)).toBeInTheDocument();
  });

  it('filters nodes by the replay cursor instead of accumulating them', () => {
    const { rerender } = render(
      <DebateTimeline events={TIMELINE_EVENTS} hasRebuttals cursorSeq={2} />,
    );

    expect(screen.getByText('Collecting evidence')).toBeInTheDocument();
    expect(screen.queryByText('Analysing')).toBeNull();
    expect(screen.queryByText('Completed')).toBeNull();

    rerender(<DebateTimeline events={TIMELINE_EVENTS} hasRebuttals cursorSeq={null} />);
    expect(screen.queryByRole('list', { name: 'Decision timeline' })).toBeNull();
    expect(screen.getByText(/Replay is at the start/)).toBeInTheDocument();
  });

  it('says when there are no stored events rather than inventing a pipeline', () => {
    render(<DebateTimeline events={[]} hasRebuttals={false} />);

    expect(screen.getByText(/No stage events were recorded/)).toBeInTheDocument();
    expect(screen.queryByRole('list', { name: 'Decision timeline' })).toBeNull();
  });
});
