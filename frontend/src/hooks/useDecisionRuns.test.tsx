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

  it('uses latest_sequence as the server tail when it differs from last_sequence', async () => {
    const all = Array.from({ length: 6 }, (_, index) =>
      runEvent({ event_id: `tail-${index + 1}`, seq: index + 1 }),
    );
    const fetchMock = vi.fn(async (input: RequestInfo | URL): Promise<Response> => {
      const url = new URL(String(input), 'http://localhost');
      const afterSeq = Number(url.searchParams.get('after_seq'));
      const page = all.filter((event) => event.seq > afterSeq).slice(0, 2);
      return jsonResponse({
        events: page,
        // The window ends at 2/4/6 while the run tail is 6 from the first page on.
        last_sequence: page.at(-1)?.seq ?? afterSeq,
        latest_sequence: 6,
        next: page.length === 2 && page.at(-1)?.seq !== 6 ? '/next' : null,
      } satisfies RunEventsResponse);
    });
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch);

    const { result } = renderHook(() => useDecisionRunEvents('run-1', 'token'));
    await waitFor(() => expect(result.current.events).toHaveLength(6));

    expect(result.current.events.map((event) => event.seq)).toEqual([1, 2, 3, 4, 5, 6]);
    expect(result.current.serverLastSequence).toBe(6);
    expect(result.current.contiguousSequence).toBe(6);
    expect(result.current.remaining).toBe(0);
    expect(result.current.catchingUp).toBe(false);
    expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(3);
  });

  it('continues past the old five-page cap when a run has 320 events', async () => {
    const all = Array.from({ length: 320 }, (_, index) =>
      runEvent({ event_id: `large-${index + 1}`, seq: index + 1 }),
    );
    const fetchMock = vi.fn(async (input: RequestInfo | URL): Promise<Response> => {
      const url = new URL(String(input), 'http://localhost');
      const afterSeq = Number(url.searchParams.get('after_seq'));
      const page = all.filter((event) => event.seq > afterSeq).slice(0, 100);
      return jsonResponse({
        events: page,
        last_sequence: page.at(-1)?.seq ?? afterSeq,
        latest_sequence: 320,
        next: page.at(-1)?.seq === 320 ? null : '/next',
      } satisfies RunEventsResponse);
    });
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch);

    const { result } = renderHook(() => useDecisionRunEvents('run-1', 'token'));
    await waitFor(() => expect(result.current.events).toHaveLength(320), { timeout: 5_000 });

    expect(result.current.contiguousSequence).toBe(320);
    expect(result.current.serverLastSequence).toBe(320);
    expect(result.current.remaining).toBe(0);
    const afterSeqValues = fetchMock.mock.calls.map((call) => {
      const url = new URL(String(call[0]), 'http://localhost');
      return Number(url.searchParams.get('after_seq'));
    });
    expect(afterSeqValues.slice(0, 4)).toEqual([0, 100, 200, 300]);
  });

  it('recovers an internal sequence gap from the last contiguous sequence', async () => {
    const pages: Record<number, RunEventsResponse> = {
      0: {
        events: [
          runEvent({ event_id: 'g1', seq: 1 }),
          runEvent({ event_id: 'g2', seq: 2 }),
          runEvent({ event_id: 'g5', seq: 5 }),
        ],
        next: null,
        last_sequence: 5,
        latest_sequence: 6,
      },
      2: {
        events: [
          runEvent({ event_id: 'g3', seq: 3 }),
          runEvent({ event_id: 'g4', seq: 4 }),
          runEvent({ event_id: 'g5', seq: 5 }),
          runEvent({ event_id: 'g6', seq: 6 }),
        ],
        next: null,
        last_sequence: 6,
        latest_sequence: 6,
      },
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL): Promise<Response> => {
      const url = new URL(String(input), 'http://localhost');
      const afterSeq = Number(url.searchParams.get('after_seq'));
      return jsonResponse(pages[afterSeq] ?? { events: [], next: null, last_sequence: afterSeq, latest_sequence: 6 });
    });
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch);

    const { result } = renderHook(() => useDecisionRunEvents('run-1', 'token'));
    await waitFor(() => expect(result.current.events).toHaveLength(6));

    expect(result.current.events.map((event) => event.seq)).toEqual([1, 2, 3, 4, 5, 6]);
    expect(result.current.contiguousSequence).toBe(6);
    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(urls[1]).toContain('after_seq=2');
    expect(urls[1]).not.toContain('after_seq=5');
  });

  it('deduplicates by event id and sorts out-of-order delivery', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL): Promise<Response> =>
      jsonResponse({
        events: [
          runEvent({ event_id: 'dup', seq: 2, payload: { message: 'newer' } }),
          runEvent({ event_id: 'e1', seq: 1 }),
          runEvent({ event_id: 'dup', seq: 2, payload: { message: 'older' } }),
          runEvent({ event_id: 'e3', seq: 3 }),
        ],
        next: null,
        last_sequence: 3,
        latest_sequence: 3,
      } satisfies RunEventsResponse),
    );
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch);

    const { result } = renderHook(() => useDecisionRunEvents('run-1', 'token'));
    await waitFor(() => expect(result.current.events).toHaveLength(3));

    expect(result.current.events.map((event) => event.event_id)).toEqual(['e1', 'dup', 'e3']);
    expect(result.current.events.map((event) => event.seq)).toEqual([1, 2, 3]);
    expect(result.current.contiguousSequence).toBe(3);
  });

  it('resyncs from the contiguous tail after an offline/online cycle', async () => {
    let online = false;
    const fetchMock = vi.fn(async (_input: RequestInfo | URL): Promise<Response> => {
      if (!online) throw new Error('offline');
      return jsonResponse({
        events: [
          runEvent({ event_id: 'r1', seq: 1 }),
          runEvent({ event_id: 'r2', seq: 2 }),
        ],
        next: null,
        last_sequence: 2,
        latest_sequence: 2,
      } satisfies RunEventsResponse);
    });
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch);

    const { result } = renderHook(() => useDecisionRunEvents('run-1', 'token'));
    await waitFor(() => expect(result.current.error).not.toBeNull());

    online = true;
    act(() => {
      window.dispatchEvent(new Event('online'));
    });

    await waitFor(() => expect(result.current.events).toHaveLength(2));
    expect(result.current.error).toBeNull();
    expect(result.current.contiguousSequence).toBe(2);
  });
});
