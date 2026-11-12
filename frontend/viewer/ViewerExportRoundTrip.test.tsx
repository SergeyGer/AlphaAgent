import fs from 'node:fs';
import path from 'node:path';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { parseViewerBundle, type ViewerRun } from './bundle';
import { ViewerRunView } from './ViewerRunView';
import { ViewerApp } from './ViewerApp';

/**
 * Step 5A and 5C — the built viewer's import path, not the fixture loader.
 *
 * The document imported here is the committed export the authenticated
 * endpoint produced (`docs/spec/fixtures/decision-run-export.v1.json`). No
 * fetch is provided; the network guard stays installed so a request would fail
 * the test rather than silently succeed.
 */

const samplePath = path.resolve(
  process.cwd(),
  '../docs/spec/fixtures/decision-run-export.v1.json',
);

function importedRun(): ViewerRun {
  const raw = JSON.parse(fs.readFileSync(samplePath, 'utf8')) as unknown;
  const run = parseViewerBundle(raw).runs[0];
  if (!run) throw new Error('The export sample did not import.');
  return run;
}

function networkProbe() {
  const calls: unknown[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = ((...args: unknown[]) => {
    calls.push(args);
    return Promise.reject(new Error('offline viewer: network access is disabled'));
  }) as typeof globalThis.fetch;
  return {
    calls,
    restore: () => {
      globalThis.fetch = original;
    },
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('ViewerRunView — import of a real captured export', () => {
  it('renders the visible run, provenance and guard events from the export', async () => {
    const probe = networkProbe();
    try {
      const run = importedRun();
      const user = userEvent.setup();
      render(<ViewerRunView run={run} />);

      expect(probe.calls).toEqual([]);
      expect(screen.getByTestId('viewer-source-label')).toHaveAttribute(
        'data-source-label',
        'real_recorded',
      );
      expect(screen.getByTestId('viewer-source-label')).toHaveTextContent('Real recorded');
      expect(screen.getByTestId('viewer-provenance')).toHaveTextContent('actually executed');
      expect(screen.getAllByText('AAPL').length).toBeGreaterThan(0);
      expect(screen.getByText(/Read-only replay/)).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /approve/i })).toBeNull();
      expect(screen.queryByRole('button', { name: /execute/i })).toBeNull();

      // The imported flat contract matches the nested verdict the API served.
      expect(run.detail.action).toBe('BUY');
      expect(run.detail.rationale).toContain('Résumé — 你好');
      expect(run.detail.decisive_claim_ids).toEqual(
        (run.raw_export?.['verdict'] as Record<string, unknown>)['decisive_claim_ids'],
      );

      await user.click(screen.getByRole('button', { name: 'Restart replay' }));

      const guardSeqs = run.events
        .filter((event) => event.type === 'guard.evaluated')
        .map((event) => event.seq);
      expect(guardSeqs).toHaveLength(2);

      // Seek to the first check: the preflight approval of 10.
      let steps = 0;
      while (screen.queryByTestId('guard-status') === null && steps < run.events.length) {
        await user.click(screen.getByRole('button', { name: 'Next event' }));
        steps += 1;
      }
      expect(screen.getByTestId('guard-status')).toHaveTextContent('Approved');
      expect(screen.getByText('Requested quantity').nextElementSibling).toHaveTextContent('10');
      expect(screen.getByText('Allowed quantity').nextElementSibling).toHaveTextContent('10');
      const firstGuardId = run.detail.guard_evaluations?.[0]?.id ?? '';
      expect(screen.getByText('Evaluation').nextElementSibling).toHaveTextContent(firstGuardId);

      // Seek across the second check: the clamp to 4 is visible only now.
      let more = 0;
      while (
        screen.getByTestId('guard-status').textContent?.trim().toLowerCase() !== 'clamped' &&
        more < run.events.length
      ) {
        await user.click(screen.getByRole('button', { name: 'Next event' }));
        more += 1;
      }
      expect(screen.getByTestId('guard-status')).toHaveTextContent('Clamped');
      expect(screen.getByText('Requested quantity').nextElementSibling).toHaveTextContent('10');
      expect(screen.getByText('Allowed quantity').nextElementSibling).toHaveTextContent('4');
      const secondGuardId = run.detail.guard_evaluations?.[1]?.id ?? '';
      expect(screen.getByText('Evaluation').nextElementSibling).toHaveTextContent(secondGuardId);
      expect(screen.getAllByText('max_trade_allocation_pct').length).toBeGreaterThan(0);
      expect(screen.getAllByText(/quantity_clamped_to_budget/).length).toBeGreaterThan(0);

      // The evidence reference survived the round trip and opens the stored
      // Unicode excerpt, not a re-fetched source.
      const opener = screen.getAllByRole('button', { name: /open evidence/i })[0];
      expect(opener).toBeDefined();
      await user.click(opener as HTMLElement);
      const dialog = screen.getByRole('dialog');
      expect(dialog).toHaveTextContent('Unicode evidence — 你好');
      expect(within(dialog).queryAllByRole('link')).toHaveLength(0);
      expect(probe.calls).toEqual([]);
    } finally {
      probe.restore();
    }
  }, 30_000);
});

describe('ViewerApp — loading a captured export through the file picker', () => {
  it('imports the export in the viewer application with no network', async () => {
    const probe = networkProbe();
    try {
      const user = userEvent.setup();
      const text = fs.readFileSync(samplePath, 'utf8');
      render(<ViewerApp />);
      const input = await screen.findByTestId('viewer-file-input');
      await user.upload(
        input,
        new File([text], 'decision-run-export.v1.json', { type: 'application/json' }),
      );
      await waitFor(() => {
        expect(screen.getByTestId('viewer-source-label')).toHaveAttribute(
          'data-source-label',
          'real_recorded',
        );
      });
      expect(screen.getByText(/1 run · Real recorded · schema 1.0 · file/)).toBeInTheDocument();
      expect(screen.getAllByText('AAPL').length).toBeGreaterThan(0);
      expect(probe.calls.length).toBeGreaterThan(0);
      // The only network attempt is the viewer's own relative fixture probe; it
      // is denied by the test's fetch stub and no external request is made.
      for (const call of probe.calls) {
        expect(String((call as unknown[])[0])).toContain('decision-runs.v1.bundle.json');
      }
    } finally {
      probe.restore();
    }
  }, 30_000);
});
