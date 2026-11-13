import fs from 'node:fs';
import path from 'node:path';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import {
  parseViewerBundle,
  type ViewerBundle,
  type ViewerRun,
} from './bundle';
import { ViewerRunView } from './ViewerRunView';
import { guard } from '../src/test/decisionRunFixtures';

/**
 * REP-02 presentation contract.
 *
 * The viewer must reuse the real detail/timeline/replay components, label the
 * source explicitly, and provide no route to execution or approval. The
 * real-recorded label is tested independently from the synthetic label: a run
 * whose mode says `recorded` must not render as synthetic, and vice versa.
 */

function bundle(): ViewerBundle {
  const file = path.resolve(process.cwd(), 'viewer/public/decision-runs.v1.bundle.json');
  return JSON.parse(fs.readFileSync(file, 'utf8')) as ViewerBundle;
}

function run(index = 0): ViewerRun {
  const imported = parseViewerBundle(bundle());
  const selected = imported.runs[index];
  if (!selected) throw new Error(`No run at index ${index}`);
  return selected;
}

describe('ViewerRunView — source labels', () => {
  it('labels the committed fixture run as synthetic', () => {
    render(<ViewerRunView run={run(0)} />);

    const label = screen.getByTestId('viewer-source-label');
    expect(label).toHaveAttribute('data-source-label', 'synthetic');
    expect(within(label).getByText('Synthetic')).toBeInTheDocument();
    expect(screen.getByTestId('viewer-provenance')).toHaveTextContent('Fixture data');
  });

  it('labels a recorded run as real recorded independently of synthetic', () => {
    const source = run(0);
    const recorded: ViewerRun = JSON.parse(JSON.stringify(source)) as ViewerRun;
    recorded.mode = 'recorded';
    recorded.source_label = 'real_recorded';
    recorded.detail.run.mode = 'recorded';
    recorded.detail.run.source = 'crewai';
    recorded.detail.run.fixture_id = null;
    recorded.provenance.label = 'real_recorded';
    recorded.provenance.fixture_id = null;
    recorded.provenance.origin_kind = 'provider_run';
    recorded.provenance.provider_backed = true;
    recorded.provenance.note = 'Provider-backed test record used to exercise the label.';
    recorded.provenance.limitations = ['The provider reported tokens but no billed dollar amount.'];

    render(<ViewerRunView run={recorded} />);

    const label = screen.getByTestId('viewer-source-label');
    expect(label).toHaveAttribute('data-source-label', 'real_recorded');
    expect(within(label).getByText('Real recorded')).toBeInTheDocument();
    expect(within(label).queryByText('Synthetic')).toBeNull();
    expect(screen.getByTestId('viewer-provenance')).toHaveTextContent('actually executed');
    expect(screen.getByTestId('viewer-run-origin')).toHaveAttribute(
      'data-origin-kind',
      'provider_run',
    );
    expect(screen.getByTestId('viewer-origin-limitations')).toHaveTextContent(
      'no billed dollar amount',
    );
  });

  it('labels heuristic and legacy runs explicitly', () => {
    const source = run(0);
    for (const mode of ['heuristic', 'legacy'] as const) {
      const variant: ViewerRun = JSON.parse(JSON.stringify(source)) as ViewerRun;
      variant.mode = mode;
      variant.source_label = mode;
      variant.detail.run.mode = mode;
      variant.provenance.label = mode;
      const view = render(<ViewerRunView run={variant} />);
      const label = screen.getByTestId('viewer-source-label');
      expect(label).toHaveAttribute('data-source-label', mode);
      expect(within(label).getByText(mode === 'heuristic' ? 'Heuristic' : 'Legacy')).toBeInTheDocument();
      view.unmount();
    }
  });
});

describe('ViewerRunView — read-only replay', () => {
  it('offers replay but no execution, approval or live-analysis control', () => {
    render(<ViewerRunView run={run(0)} />);

    expect(screen.getByRole('button', { name: 'Play replay' })).toBeInTheDocument();
    expect(screen.getByText(/Read-only replay/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /approve/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /execute/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /run agent/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /toggle autonomy/i })).toBeNull();
  });

  it('keeps the verdict out of the scene before adjudication and pins the final summary separately', async () => {
    const user = userEvent.setup();
    render(<ViewerRunView run={run(0)} />);

    await user.click(screen.getByRole('button', { name: 'Restart replay' }));

    expect(screen.getByTestId('viewer-pinned-final-summary')).toHaveTextContent(
      'Final summary',
    );
    expect(screen.getByText(/Replay has not reached adjudication/)).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Full CIO rationale' })).toBeNull();
    expect(screen.queryByRole('article', { name: '30-day horizon outcome' })).toBeNull();
  });

  it('navigates every event forward and backward without drifting', async () => {
    const user = userEvent.setup();
    render(<ViewerRunView run={run(1)} />);

    await user.click(screen.getByRole('button', { name: 'Restart replay' }));
    const start = document.body.textContent;
    expect(screen.getByText(/Replay at start/)).toBeInTheDocument();

    const total = run(1).events.length;
    for (let index = 0; index < total; index += 1) {
      await user.click(screen.getByRole('button', { name: 'Next event' }));
    }
    expect(screen.getByText(/End of recorded events/)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(`Event ${total} of ${total}`))).toBeInTheDocument();

    for (let index = 0; index < total; index += 1) {
      await user.click(screen.getByRole('button', { name: 'Previous event' }));
    }
    expect(document.body.textContent).toBe(start);
  }, 20_000);

  it('never renders an external HTTP source link as an anchor', async () => {
    const user = userEvent.setup();
    const source = run(0);
    const hostile: ViewerRun = JSON.parse(JSON.stringify(source)) as ViewerRun;
    for (const item of hostile.detail.evidence) {
      item.source_uri = 'https://provider.example/secret';
    }
    render(<ViewerRunView run={hostile} />);

    const opener = screen.getAllByRole('button', { name: /open evidence/i })[0];
    expect(opener).toBeDefined();
    await user.click(opener as HTMLElement);

    const dialog = screen.getByRole('dialog');
    expect(within(dialog).queryAllByRole('link')).toHaveLength(0);
    expect(dialog).toHaveTextContent(/external link disabled/);
    await user.click(screen.getByRole('button', { name: 'Close evidence drawer' }));
  });
});


describe('ViewerRunView — guard replay', () => {
  it('shows the preflight approval of 10 until the rejection event is visible', async () => {
    const user = userEvent.setup();
    const source = run(0);
    const offline: ViewerRun = JSON.parse(JSON.stringify(source)) as ViewerRun;
    const preflight = guard({
      id: 'guard-preflight',
      status: 'approved',
      requested_quantity: '10',
      allowed_quantity: '10',
      binding_rule: 'max_trade_allocation_pct',
    });
    const final = guard({
      id: 'guard-final',
      status: 'rejected',
      requested_quantity: '10',
      allowed_quantity: '0',
      binding_rule: 'approval_terms',
    });
    offline.events = [
      {
        event_id: 'off-decision',
        seq: 1,
        type: 'decision.proposed',
        stage: 'adjudicating',
        agent_role: null,
        occurred_at: '2026-11-10T14:10:00Z',
        payload: { action: 'BUY' },
      },
      {
        event_id: 'off-guard-preflight',
        seq: 2,
        type: 'guard.evaluated',
        stage: 'validating',
        agent_role: null,
        occurred_at: '2026-11-10T14:10:01Z',
        payload: { guard_id: 'guard-preflight', status: 'approved' },
      },
      {
        event_id: 'off-guard-final',
        seq: 3,
        type: 'guard.evaluated',
        stage: 'validating',
        agent_role: null,
        occurred_at: '2026-11-10T14:10:04Z',
        payload: { guard_id: 'guard-final', status: 'rejected' },
      },
      {
        event_id: 'off-execution',
        seq: 4,
        type: 'execution.rejected',
        stage: 'completed',
        agent_role: null,
        occurred_at: '2026-11-10T14:10:05Z',
        payload: { execution_state: 'rejected' },
      },
    ];
    offline.detail.guard = final;
    offline.detail.guards = [preflight, final];
    offline.detail.guard_evaluations = [preflight, final];
    offline.detail.outcomes = [];

    render(<ViewerRunView run={offline} />);
    await user.click(screen.getByRole('button', { name: 'Restart replay' }));

    // Before the first check: no guard card at all.
    expect(screen.queryByTestId('guard-status')).toBeNull();

    await user.click(screen.getByRole('button', { name: 'Next event' }));
    expect(screen.getByText(/Replay has not reached validation/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Next event' }));
    expect(screen.getByTestId('guard-status')).toHaveTextContent('Approved');
    expect(screen.getByText('Requested quantity').nextElementSibling).toHaveTextContent('10');
    expect(screen.getByText('Allowed quantity').nextElementSibling).toHaveTextContent('10');

    await user.click(screen.getByRole('button', { name: 'Next event' }));
    expect(screen.getByTestId('guard-status')).toHaveTextContent('Rejected');
    expect(screen.getByText('Allowed quantity').nextElementSibling).toHaveTextContent('0');
  });
});
