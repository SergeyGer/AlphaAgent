import { useCallback, useEffect, useRef, useState } from 'react';
import { errorMessage, isAbortError } from '../api/client';

export interface AsyncResource<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  refresh: () => void;
  /** Locally patch the cached value (used for WebSocket-driven updates). */
  setData: (updater: (previous: T | null) => T | null) => void;
}

/**
 * Small fetch-on-mount primitive: aborts in-flight requests on unmount/dep
 * change, tracks loading + error state, and exposes a manual `refresh()`.
 *
 * `key` identifies the request; when it changes the resource is refetched.
 */
export function useAsyncResource<T>(
  key: string,
  loader: (signal: AbortSignal) => Promise<T>,
  enabled = true,
): AsyncResource<T> {
  const loaderRef = useRef(loader);
  useEffect(() => {
    loaderRef.current = loader;
  });

  const [data, setInnerData] = useState<T | null>(null);
  const [loading, setLoading] = useState<boolean>(enabled);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  const refresh = useCallback(() => {
    setNonce((value) => value + 1);
  }, []);

  const setData = useCallback((updater: (previous: T | null) => T | null) => {
    setInnerData((previous) => updater(previous));
  }, []);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }

    const controller = new AbortController();
    let active = true;

    setLoading(true);
    setError(null);

    loaderRef
      .current(controller.signal)
      .then((result) => {
        if (!active) return;
        setInnerData(result);
        setLoading(false);
      })
      .catch((cause: unknown) => {
        if (!active || isAbortError(cause)) return;
        setError(errorMessage(cause));
        setLoading(false);
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [key, enabled, nonce]);

  return { data, loading, error, refresh, setData };
}
