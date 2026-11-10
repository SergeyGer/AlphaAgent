import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { GuardComparison } from './GuardComparison';
import { guard, guardCheck } from '../test/decisionRunFixtures';

/**
 * The guard table has three outcomes, not two. A `not_evaluated` check is a
 * statement that the guard never ran the rule — rendering it as `Fail` would
 * accuse the order of failing a test that was never applied.
 */

describe('GuardComparison', () => {
  it('renders requested and allowed quantities, status, rule and reason codes', () => {
    render(<GuardComparison guard={guard()} />);

    expect(screen.getByTestId('guard-status')).toHaveTextContent('Clamped');
    expect(screen.getByText('Requested quantity').nextElementSibling).toHaveTextContent('100');
    expect(screen.getByText('Allowed quantity').nextElementSibling).toHaveTextContent('40');
    expect(screen.getByText('Binding rule').nextElementSibling).toHaveTextContent('MAX_POSITION_PCT');
    expect(screen.getByText('POSITION_LIMIT')).toBeInTheDocument();
    expect(screen.getByText(/1 pass · 1 fail · 1 not evaluated/)).toBeInTheDocument();
  });

  it('renders not_evaluated distinctly from fail', () => {
    render(<GuardComparison guard={guard()} />);

    const fail = screen.getByText('Fail');
    const notEvaluated = screen.getByText('Not evaluated');

    expect(fail).toHaveAttribute('data-outcome', 'fail');
    expect(notEvaluated).toHaveAttribute('data-outcome', 'not_evaluated');
    expect(fail).not.toBe(notEvaluated);

    // They carry different labels, different data and different tones.
    expect(fail.className).toContain('rose');
    expect(notEvaluated.className).toContain('amber');
    expect(notEvaluated.className).not.toContain('rose');

    // The never-evaluated row is not counted as, or labelled, a failure.
    const row = notEvaluated.closest('tr');
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).queryByText('Fail')).toBeNull();
    expect(within(row as HTMLElement).getAllByText('Not available')).toHaveLength(2);
  });

  it('summarises all three outcomes without folding them together', () => {
    render(<GuardComparison guard={guard()} />);

    expect(screen.getByText(/3 total · 1 pass · 1 fail · 1 not evaluated/)).toBeInTheDocument();
    expect(screen.getAllByText('Pass')).toHaveLength(1);
    expect(screen.getAllByText('Fail')).toHaveLength(1);
    expect(screen.getAllByText('Not evaluated')).toHaveLength(1);
  });

  it('renders an approved guard with its label and every check', () => {
    render(
      <GuardComparison
        guard={guard({
          status: 'approved',
          requested_quantity: '40',
          allowed_quantity: '40',
          binding_rule: null,
          reason_codes: [],
          checks: [guardCheck()],
        })}
      />,
    );

    expect(screen.getByTestId('guard-status')).toHaveTextContent('Approved');
    expect(screen.getByText('No binding rule recorded')).toBeInTheDocument();
    expect(screen.getByText('No reason codes recorded')).toBeInTheDocument();
    expect(screen.getByText('Pass')).toBeInTheDocument();
  });

  it('says when there were no checks rather than implying everything passed', () => {
    render(<GuardComparison guard={guard({ checks: [] })} />);

    expect(screen.getByText('No guard checks were recorded, so none are presented as passing.')).toBeInTheDocument();
    expect(screen.getByText('0 total · 0 pass · 0 fail · 0 not evaluated')).toBeInTheDocument();
  });

  it('renders an explicit absent state when no guard comparison exists', () => {
    const { container } = render(<GuardComparison guard={null} />);

    expect(container.querySelector('[data-state="guard-unavailable"]')).toHaveTextContent(
      'Guard comparison not available',
    );
    expect(screen.queryByText('Approved')).toBeNull();
    expect(screen.queryByText('Requested quantity')).toBeNull();
  });
});
