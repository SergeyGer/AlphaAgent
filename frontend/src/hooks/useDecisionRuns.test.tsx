import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useDecisionRunEvents } from './useDecisionRuns';
import { runEvent } from '../test/decisionRunFixtures';
import type { RunEventsResponse } from '../types';

/**
 * The events endpoint is incremental. When the first page reports a higher
 * `last_sequence` than it delivered, the hook must recover through
 * `after_seq=<local tail>` rather than resetting to zero.
 */

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('useDecisionRunEvents', () => {
  it('recovers a gap with after_seq instead of refetching from zero', async () => {
    const pageOne: RunEventsResponse = {
      events: [
        runEvent({ event_id: 'e1', seq: 1 }),
        runEvent({ event_id: 'e2', seq: 2 }),
      ],
      next: null,
      last_sequence: 4,
    };
    const pageTwo: RunEventsResponse = {
      events: [
        runEvent({ event_id: 'e3', seq: 3 }),
        runEvent({ event_id: 'e4', seq: 4 }),
      ],
      next: null,
      last_sequence: 4,
    };

    const fetchMock = vi.fn(async (input: RequestInfo | URL): Promise<Response> => {
      const url = new URL(String(input), 'http://localhost');
      const afterSeq = Number(url.searchParams.get('after_seq'));
      return jsonResponse(afterSeq <= 0 ? pageOne : pageTwo);
    });
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch);

    const { result } = renderHook(() => useDecisionRunEvents('run-1', 'token'));

    await waitFor(() => expect(result.current.events).toHaveLength(4));

    expect(result.current.events.map((event) => event.seq)).toEqual([1, 2, 3, 4]);
    expect(result.current.lastSequence).toBe(4);
    expect(result.current.serverLastSequence).toBe(4);
    expect(result.current.hasMore).toBe(false);
    expect(result.current.error).toBeNull();

    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(urls).toHaveLength(2);
    expect(urls[0]).toContain('/api/decision-runs/run-1/events/?after_seq=0');
    expect(urls[1]).toContain('after_seq=2');
  });

  it('merges a refresh by event id so old events are not duplicated', async () => {
    let call = 0;
    const fetchMock = vi.fn(async (_input: RequestInfo | URL): Promise<Response> => {
      call += 1;
      const response: RunEventsResponse =
        call === 1
          ? {
              events: [
                runEvent({ event_id: 'e1', seq: 1 }),
                runEvent({ event_id: 'e2', seq: 2 }),
              ],
              next: null,
              last_sequence: 2,
            }
          : {
              events: [
                runEvent({ event_id: 'e2', seq: 2 }),
                runEvent({ event_id: 'e3', seq: 3 }),
              ],
              next: null,
              last_sequence: 3,
            };
      return jsonResponse(response);
    });
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch);

    const { result } = renderHook(() => useDecisionRunEvents('run-1', 'token'));
    await waitFor(() => expect(result.current.events).toHaveLength(2));

    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.events).toHaveLength(3));

    expect(result.current.events.map((event) => event.event_id)).toEqual(['e1', 'e2', 'e3']);
    const secondUrl = String(fetchMock.mock.calls[1]?.[0] ?? '');
    expect(secondUrl).toContain('after_seq=2');
  });
});
