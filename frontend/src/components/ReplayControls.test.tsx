import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState, type ReactElement } from 'react';
import { describe, expect, it } from 'vitest';
import { DebateTimeline } from './DebateTimeline';
import { ReplayControls } from './ReplayControls';
import { TIMELINE_EVENTS } from '../test/decisionRunFixtures';

/**
 * REP-01 — deterministic, keyboard-operable replay.
 *
 * State at a sequence is a pure function of the event list and the cursor, so
 * moving forward, backward and forward again must produce byte-identical
 * screens. The "Recorded run" label is part of the controls at every state.
 */

function Harness(): ReactElement {
  const [cursorSeq, setCursorSeq] = useState<number | null>(null);

  return (
    <div data-testid="replay-harness">
      <ReplayControls
        events={TIMELINE_EVENTS}
        hasRebuttals
        cursorSeq={cursorSeq}
        onCursorSeqChange={setCursorSeq}
      />
      <DebateTimeline events={TIMELINE_EVENTS} hasRebuttals cursorSeq={cursorSeq} />
    </div>
  );
}

function capture(): string {
  return screen.getByTestId('replay-harness').textContent ?? '';
}

describe('ReplayControls', () => {
  it('lands on exactly the same state after forward, backward, forward navigation', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    const startState = capture();
    expect(startState).toContain('Replay at start');

    for (let step = 0; step < 4; step += 1) {
      await user.click(screen.getByRole('button', { name: 'Next event' }));
    }
    const forwardState = capture();
    expect(forwardState).not.toBe(startState);
    expect(
      within(screen.getByRole('list', { name: 'Decision timeline' })).getByText('Analysing'),
    ).toBeInTheDocument();

    for (let step = 0; step < 4; step += 1) {
      await user.click(screen.getByRole('button', { name: 'Previous event' }));
    }
    expect(capture()).toBe(startState);

    for (let step = 0; step < 4; step += 1) {
      await user.click(screen.getByRole('button', { name: 'Next event' }));
    }
    expect(capture()).toBe(forwardState);
  });

  it('keeps the Recorded run label visible through every control state', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    expect(screen.getByTestId('recorded-run-label')).toHaveTextContent('Recorded run');

    await user.click(screen.getByRole('button', { name: 'Next event' }));
    expect(screen.getByTestId('recorded-run-label')).toHaveTextContent('Recorded run');

    await user.click(screen.getByRole('button', { name: 'Previous event' }));
    expect(screen.getByTestId('recorded-run-label')).toHaveTextContent('Recorded run');

    await user.click(screen.getByRole('button', { name: 'Play replay' }));
    expect(screen.getByTestId('recorded-run-label')).toHaveTextContent('Recorded run');
    expect(screen.getByRole('button', { name: 'Pause replay' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Pause replay' }));
  });

  it('disables previous/restart at the start and enables them after one step', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    expect(screen.getByRole('button', { name: 'Previous event' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Restart replay' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Next event' })).toBeEnabled();

    await user.click(screen.getByRole('button', { name: 'Next event' }));

    expect(screen.getByRole('button', { name: 'Previous event' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Restart replay' })).toBeEnabled();
  });

  it('offers direct stage selection and 0.5x/1x/2x speeds', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    const normal = screen.getByRole('button', { name: 'Playback speed 1x' });
    const fast = screen.getByRole('button', { name: 'Playback speed 2x' });
    expect(normal).toHaveAttribute('aria-pressed', 'true');
    await user.click(fast);
    expect(fast).toHaveAttribute('aria-pressed', 'true');
    expect(normal).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByRole('button', { name: 'Playback speed 0.5x' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Jump to stage Validating' }));

    const timeline = screen.getByRole('list', { name: 'Decision timeline' });
    expect(within(timeline).getByText('Validating')).toBeInTheDocument();
    expect(within(timeline).queryByText('Completed')).toBeNull();
  });

  it('makes every replay control reachable and operable from the keyboard', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    const play = screen.getByRole('button', { name: 'Play replay' });
    play.focus();
    expect(play).toHaveFocus();
    expect(play.className).toContain('focus-visible:ring-2');

    // Tab from play lands on the next enabled control; disabled ones are skipped.
    await user.tab();
    const next = screen.getByRole('button', { name: 'Next event' });
    expect(next).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(next).toHaveFocus();
    expect(screen.getByText(/Event 1 of 9/)).toBeInTheDocument();

    await user.tab();
    expect(screen.getByRole('button', { name: 'Restart replay' })).toHaveFocus();

    const fast = screen.getByRole('button', { name: 'Playback speed 2x' });
    fast.focus();
    await user.keyboard('{Enter}');
    expect(fast).toHaveAttribute('aria-pressed', 'true');

    const stage = screen.getByRole('button', { name: 'Jump to stage Validating' });
    stage.focus();
    await user.keyboard('{Enter}');
    expect(stage).toHaveFocus();
    expect(
      within(screen.getByRole('list', { name: 'Decision timeline' })).getByText('Validating'),
    ).toBeInTheDocument();
  });

  it('announces stage changes only, not every token event', async () => {
    const user = userEvent.setup();
    const { container } = render(<Harness />);

    const liveRegion = container.querySelector('[role="status"][aria-live="polite"]');
    expect(liveRegion).toHaveTextContent('Replay at start.');

    await user.click(screen.getByRole('button', { name: 'Next event' }));
    await user.click(screen.getByRole('button', { name: 'Next event' }));
    expect(liveRegion).toHaveTextContent('Stage Collecting evidence.');

    // Two more steps reach analysing, the first actual stage change.
    await user.click(screen.getByRole('button', { name: 'Next event' }));
    await user.click(screen.getByRole('button', { name: 'Next event' }));
    expect(liveRegion).toHaveTextContent('Stage Analysing.');
  });

  it('shows an honest empty state when there are no recorded events', () => {
    render(
      <ReplayControls events={[]} cursorSeq={null} onCursorSeqChange={() => undefined} />,
    );

    expect(screen.getByText(/No recorded events to play/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Play replay' })).toBeDisabled();
    expect(screen.getByTestId('recorded-run-label')).toHaveTextContent('Recorded run');
  });
});
