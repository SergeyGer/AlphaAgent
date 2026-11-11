import { useCallback, useEffect, useRef, useState } from 'react';
import { api, errorMessage, isAbortError } from '../api/client';
import {
  lastContiguousSequence,
  lastEventSequence,
  mergeRunEvents,
} from '../lib/decisionRuns';
import type { DecisionDetailPayload, RunEvent, RunSummary } from '../types';
import { useAsyncResource, type AsyncResource } from './useAsyncResource';

/**
 * Decision-run data for the Phase 1 explain screen.
 *
 * The list and detail resources are ordinary abortable fetches. The event
 * stream is deliberately not: it is incremental (`after_seq`) and recovers from
 * the last *contiguous* sequence it holds, not the highest sequence. Those are
 * different after an internal gap: with events 1, 2 and 5 locally, asking for
 * `after_seq=5` would leave 3 and 4 missing forever.
 *
 * A page can stop before the run's tail, so the endpoint's `latest_sequence` is
 * the only authoritative "caught up" signal. `last_sequence` is the end of the
 * window. The recovery loop pages until the local contiguous tail reaches the
 * server tail, or until it can no longer make progress; in the latter case the
 * remaining count is exposed rather than hidden behind an arbitrary page cap.
 */

/** Safety valve; not a normal path. Reaching it is reported as remaining work. */
export const MAX_RECOVERY_PAGES = 500;

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
   * a completed/recorded run is fetched once.
   */
  pollIntervalMs?: number;
}

export interface DecisionRunEventsState {
  events: RunEvent[];
  loading: boolean;
  error: string | null;
  /** Highest sequence number held locally. */
  lastSequence: number | null;
  /** Last sequence in the unbroken run starting at 1 (the recovery cursor). */
  contiguousSequence: number | null;
  /** The server's newest persisted sequence (`latest_sequence`), not the page end. */
  serverLastSequence: number | null;
  /** Events at or below the server tail that are still not held locally. */
  remaining: number;
  /** True when the server reports work beyond the local contiguous tail. */
  hasMore: boolean;
  /** True when the recovery loop stopped before catching up (gap or page cap). */
  catchingUp: boolean;
  /** Recover from the local tail using `after_seq` (also retries an error). */
  refresh: () => void;
}

function responseLatest(response: {
  latest_sequence?: unknown;
  last_sequence?: unknown;
  events?: RunEvent[];
}): number | null {
  const latest = response?.latest_sequence;
  if (typeof latest === 'number' && Number.isFinite(latest)) return latest;

  // Backward compatibility with producers that only published the window end.
  // It is a lower bound, not the tail; the recovery loop continues while a
  // `next` page exists even when this bound has been reached.
  const windowEnd = response?.last_sequence;
  const pageMax = lastEventSequence(
    Array.isArray(response?.events) ? response.events.filter(Boolean) : [],
  );
  const candidates = [windowEnd, pageMax].filter(
    (value): value is number => typeof value === 'number' && Number.isFinite(value),
  );
  return candidates.length === 0 ? null : Math.max(...candidates);
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
  const [contiguousSequence, setContiguousSequence] = useState<number | null>(null);
  const [serverLastSequence, setServerLastSequence] = useState<number | null>(null);
  const [remaining, setRemaining] = useState(0);
  const [catchingUp, setCatchingUp] = useState(false);
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
    setContiguousSequence(null);
    setServerLastSequence(null);
    setRemaining(0);
    setCatchingUp(false);
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
        let after = lastContiguousSequence(eventsRef.current);
        let pages = 0;
        let latestKnown: number | null = null;

        while (pages < MAX_RECOVERY_PAGES) {
          pages += 1;
          const response = await api.decisionRunEvents(runId, after, controller.signal);
          if (cancelled) return;

          const page = Array.isArray(response?.events) ? response.events.filter(Boolean) : [];
          const previousCount = eventsRef.current.length;
          const previousContiguous = lastContiguousSequence(eventsRef.current);
          const merged = mergeRunEvents(eventsRef.current, page);
          eventsRef.current = merged;

          const localLast = lastEventSequence(merged);
          const localContiguous = lastContiguousSequence(merged);
          const reportedLatest = responseLatest(response);
          if (reportedLatest !== null) {
            latestKnown = latestKnown === null ? reportedLatest : Math.max(latestKnown, reportedLatest);
          }
          const serverLast = latestKnown;

          setEvents(merged);
          setLastSequence(localLast);
          setContiguousSequence(localContiguous);
          setServerLastSequence(serverLast);
          setRemaining(serverLast === null ? 0 : Math.max(0, serverLast - localContiguous));

          const advanced =
            merged.length > previousCount || localContiguous > previousContiguous;
          const wantsMore =
            (serverLast !== null && localContiguous < serverLast) ||
            (response?.next !== null && response?.next !== undefined);
          const nextAfter = localContiguous;

          // Stop when caught up, when the page made no progress (an internal
          // gap the server cannot fill), or when the server returned nothing.
          if (!wantsMore || page.length === 0 || !advanced || nextAfter === after) break;
          after = nextAfter;
        }

        const contiguous = lastContiguousSequence(eventsRef.current);
        const tail = latestKnown ?? lastEventSequence(eventsRef.current);
        const left = tail === null ? 0 : Math.max(0, tail - contiguous);
        setRemaining(left);
        setCatchingUp(left > 0);
      } catch (cause) {
        if (!cancelled && !isAbortError(cause)) {
          setError(errorMessage(cause));
          setCatchingUp(false);
        }
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

  // A browser that reconnects has likely missed persisted events. Resync from
  // the contiguous tail instead of waiting for the next poll (which may be 0).
  useEffect(() => {
    if (!active) return undefined;
    const resync = (): void => setNonce((value) => value + 1);
    const onVisibility = (): void => {
      if (document.visibilityState === 'visible') resync();
    };
    window.addEventListener('online', resync);
    window.addEventListener('offline', resync);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.removeEventListener('online', resync);
      window.removeEventListener('offline', resync);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [active]);

  const refresh = useCallback(() => {
    setNonce((value) => value + 1);
  }, []);

  const hasMore = remaining > 0;

  return {
    events,
    loading,
    error,
    lastSequence,
    contiguousSequence,
    serverLastSequence,
    remaining,
    hasMore,
    catchingUp,
    refresh,
  };
}
