import { useCallback, useEffect, useRef, useState } from 'react';
import { api, errorMessage, isAbortError } from '../api/client';
import { lastEventSequence, mergeRunEvents } from '../lib/decisionRuns';
import type { DecisionDetailPayload, RunEvent, RunSummary } from '../types';
import { useAsyncResource, type AsyncResource } from './useAsyncResource';

/**
 * Decision-run data for the Phase 1 explain screen.
 *
 * The list and detail resources are ordinary abortable fetches. The event
 * stream is deliberately not: it is incremental (`after_seq`), it recovers from
 * a partial stream by asking for everything after the last sequence it holds,
 * and it can be polled while a run is still active. A dropped page therefore
 * re-syncs without resetting the replay cursor or re-rendering old events.
 */

/** `GET /api/decision-runs/` — newest-first list is the backend's concern. */
export function useDecisionRuns(token: string | null): AsyncResource<RunSummary[]> {
  const loader = useCallback(async (signal: AbortSignal): Promise<RunSummary[]> => {
    const body = await api.decisionRuns(signal);
    return Array.isArray(body?.results) ? body.results : [];
  }, []);

  return useAsyncResource<RunSummary[]>('decision-runs', loader, token !== null);
}

/** `GET /api/decision-runs/{id}/detail/` — disabled until a run is selected. */
export function useDecisionRun(
  runId: string | null,
  token: string | null,
): AsyncResource<DecisionDetailPayload> {
  const loader = useCallback(
    async (signal: AbortSignal): Promise<DecisionDetailPayload> => {
      if (!runId) throw new Error('No decision run selected.');
      return api.decisionRunDetail(runId, signal);
    },
    [runId],
  );

  return useAsyncResource<DecisionDetailPayload>(
    `decision-run:${runId ?? 'none'}`,
    loader,
    token !== null && runId !== null,
  );
}

export interface UseDecisionRunEventsOptions {
  /** Disable fetching entirely (e.g. no run selected). Default true. */
  enabled?: boolean;
  /**
   * Poll interval for an in-flight run. `0` (the default) disables polling so
   * a completed/recorded run is fetched exactly once.
   */
  pollIntervalMs?: number;
}

export interface DecisionRunEventsState {
  events: RunEvent[];
  loading: boolean;
  error: string | null;
  /** Highest sequence number held locally. */
  lastSequence: number | null;
  /** The server's latest sequence number, used to detect a gap. */
  serverLastSequence: number | null;
  /** True when the server says there are events newer than the local tail. */
  hasMore: boolean;
  /** Recover from the local tail using `after_seq` (also retries an error). */
  refresh: () => void;
}

export function useDecisionRunEvents(
  runId: string | null,
  token: string | null,
  options: UseDecisionRunEventsOptions = {},
): DecisionRunEventsState {
  const { enabled = true, pollIntervalMs = 0 } = options;
  const active = enabled && runId !== null && token !== null;

  const [events, setEvents] = useState<RunEvent[]>([]);
  const [loading, setLoading] = useState(active);
  const [error, setError] = useState<string | null>(null);
  const [lastSequence, setLastSequence] = useState<number | null>(null);
  const [serverLastSequence, setServerLastSequence] = useState<number | null>(null);
  const [nonce, setNonce] = useState(0);

  // The ref is the synchronous source of truth for the incremental cursor: the
  // recovery loop reads it between awaits, where state would still be stale.
  const eventsRef = useRef<RunEvent[]>([]);
  const runKey = `${runId ?? ''}|${token ? 'authenticated' : 'anonymous'}`;

  // A different run must not inherit the previous run's cursor or events.
  useEffect(() => {
    eventsRef.current = [];
    setEvents([]);
    setLastSequence(null);
    setServerLastSequence(null);
    setError(null);
    setLoading(active);
    // `active` is intentionally excluded: it is derived from the run identity
    // and token, both captured in `runKey`; including it would clear live
    // events whenever a detail poll toggled a derived flag.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runKey]);

  useEffect(() => {
    if (!active || !runId) {
      setLoading(false);
      return;
    }

    const controller = new AbortController();
    let cancelled = false;
    const firstLoad = eventsRef.current.length === 0;
    if (firstLoad) setLoading(true);
    setError(null);

    void (async () => {
      try {
        let after = lastEventSequence(eventsRef.current) ?? 0;
        let attempts = 0;

        // One response normally contains every event after `after_seq`. The
        // loop exists for the partial case: if the server reports a higher
        // `last_sequence` (or a `next` page), ask again from the new tail
        // rather than leaving a hole in the stream.
        while (attempts < 5) {
          attempts += 1;
          const response = await api.decisionRunEvents(runId, after, controller.signal);
          if (cancelled) return;

          const page = Array.isArray(response?.events) ? response.events : [];
          const merged = mergeRunEvents(eventsRef.current, page);
          eventsRef.current = merged;

          const localLast = lastEventSequence(merged);
          const serverLast =
            typeof response?.last_sequence === 'number' && Number.isFinite(response.last_sequence)
              ? response.last_sequence
              : localLast;

          setEvents(merged);
          setLastSequence(localLast);
          setServerLastSequence(serverLast);

          const nextAfter = localLast ?? 0;
          const gap =
            serverLast !== null && localLast !== null && serverLast > localLast;
          const more = response?.next !== null && response?.next !== undefined ? true : gap;

          // Stop when caught up, when a page made no progress, or when the
          // server returned nothing for the requested cursor.
          if (!more || page.length === 0 || nextAfter <= after) break;
          after = nextAfter;
        }
      } catch (cause) {
        if (!cancelled && !isAbortError(cause)) setError(errorMessage(cause));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [active, runId, token, nonce]);

  // Poll only while the caller says the run is active.
  useEffect(() => {
    if (!active || pollIntervalMs <= 0) return undefined;
    const timer = window.setInterval(() => {
      setNonce((value) => value + 1);
    }, pollIntervalMs);
    return () => window.clearInterval(timer);
  }, [active, pollIntervalMs]);

  const refresh = useCallback(() => {
    setNonce((value) => value + 1);
  }, []);

  const hasMore =
    serverLastSequence !== null && lastSequence !== null && serverLastSequence > lastSequence;

  return {
    events,
    loading,
    error,
    lastSequence,
    serverLastSequence,
    hasMore,
    refresh,
  };
}
