import { describe, expect, it } from 'vitest';
import { parseHashRoute } from './useHashRoute';

describe('parseHashRoute', () => {
  it('routes the run list and a specific run', () => {
    expect(parseHashRoute('#/runs')).toEqual({ view: 'runs', runId: null });
    expect(parseHashRoute('#/runs/run-20261110-0001')).toEqual({
      view: 'runs',
      runId: 'run-20261110-0001',
    });
  });

  it('decodes a percent-encoded id', () => {
    expect(parseHashRoute('#/runs/run%20with%20spaces')).toEqual({
      view: 'runs',
      runId: 'run with spaces',
    });
  });

  it('falls back to the dashboard for anything else', () => {
    expect(parseHashRoute('')).toEqual({ view: 'dashboard' });
    expect(parseHashRoute('#/')).toEqual({ view: 'dashboard' });
    expect(parseHashRoute('#/portfolio')).toEqual({ view: 'dashboard' });
  });
});
