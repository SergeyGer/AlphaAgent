import { useCallback, useEffect, useRef, useState } from 'react';
import { buildPortfolioSocketUrl } from '../api/client';
import { parseStreamMessage } from '../lib/stream';
import type { ConnectionStatus, StreamMessage } from '../types';

const INITIAL_RETRY_MS = 1_000;
const MAX_RETRY_MS = 30_000;
const JITTER_RATIO = 0.2;
/**
 * Consecutive failed attempts after which the hook stops reconnecting and
 * reports the terminal `'offline'` status. Without it a dead endpoint would
 * show RECONNECTING forever, because the backoff itself is capped.
 */
const MAX_RECONNECT_ATTEMPTS = 8;
/**
 * Application close codes (4000–4999) that a retry can never fix.
 * `4401` is the consumer's "token rejected" code (`core/consumers.py`): the
 * socket is accepted and then closed, so retrying only loops.
 */
const TERMINAL_CLOSE_CODES: ReadonlySet<number> = new Set<number>([4401]);

export interface UsePortfolioStreamOptions {
  token: string | null;
  onMessage: (message: StreamMessage) => void;
  /** Fired after every successful open; `isReconnect` is false for the first. */
  onOpen?: (isReconnect: boolean) => void;
}

export interface UsePortfolioStreamResult {
  status: ConnectionStatus;
  /** Reconnect attempts since the last successful open. */
  attempt: number;
  /** Epoch ms of the next scheduled reconnect, or `null` when connected. */
  nextRetryAt: number | null;
  lastMessageAt: number | null;
  /** Force an immediate reconnect (skips the remaining backoff delay). */
  reconnect: () => void;
}

/**
 * Single owner of the `/ws/portfolio/` socket.
 *
 * - reconnects with exponential backoff (1s → 30s, ±20% jitter)
 * - gives up on a terminal close code (4401 = rejected token) or after
 *   {@link MAX_RECONNECT_ATTEMPTS} consecutive failures, and then reports the
 *   terminal `'offline'` status; `reconnect()` restarts the loop manually
 * - validates every frame against the frozen contract
 * - reports opens so the app can refetch REST state after a reconnect
 */
export function usePortfolioStream({
  token,
  onMessage,
  onOpen,
}: UsePortfolioStreamOptions): UsePortfolioStreamResult {
  const [status, setStatus] = useState<ConnectionStatus>(token ? 'connecting' : 'idle');
  const [attempt, setAttempt] = useState(0);
  const [nextRetryAt, setNextRetryAt] = useState<number | null>(null);
  const [lastMessageAt, setLastMessageAt] = useState<number | null>(null);

  // Keep the latest callbacks without tearing down the socket on every render.
  const onMessageRef = useRef(onMessage);
  const onOpenRef = useRef(onOpen);
  useEffect(() => {
    onMessageRef.current = onMessage;
    onOpenRef.current = onOpen;
  });

  const reconnectRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    if (!token) {
      setStatus('idle');
      setAttempt(0);
      setNextRetryAt(null);
      reconnectRef.current = null;
      return;
    }

    const authToken: string = token;
    let disposed = false;
    let socket: WebSocket | null = null;
    let retryTimer: number | null = null;
    let attemptCount = 0;
    let hasConnected = false;

    const clearRetryTimer = () => {
      if (retryTimer !== null) {
        window.clearTimeout(retryTimer);
        retryTimer = null;
      }
    };

    /** Terminal state: stop retrying and let the indicator offer a manual Retry. */
    const goOffline = (): void => {
      clearRetryTimer();
      setNextRetryAt(null);
      setStatus('offline');
    };

    const scheduleReconnect = () => {
      if (disposed) return;
      // Give up rather than count down forever once the attempts are exhausted.
      if (attemptCount >= MAX_RECONNECT_ATTEMPTS) {
        goOffline();
        return;
      }
      const backoff = Math.min(MAX_RETRY_MS, INITIAL_RETRY_MS * 2 ** attemptCount);
      const jitter = backoff * JITTER_RATIO * (Math.random() * 2 - 1);
      const delay = Math.max(500, Math.round(backoff + jitter));
      attemptCount += 1;
      setAttempt(attemptCount);
      setNextRetryAt(Date.now() + delay);
      setStatus('reconnecting');
      retryTimer = window.setTimeout(() => {
        retryTimer = null;
        connect();
      }, delay);
    };

    function connect(): void {
      if (disposed) return;
      clearRetryTimer();
      setNextRetryAt(null);
      setStatus(hasConnected ? 'reconnecting' : 'connecting');

      let next: WebSocket;
      try {
        next = new WebSocket(buildPortfolioSocketUrl(authToken));
      } catch {
        scheduleReconnect();
        return;
      }

      socket = next;

      next.onopen = () => {
        if (disposed) {
          next.close(1000, 'unmounted');
          return;
        }
        const isReconnect = hasConnected;
        hasConnected = true;
        attemptCount = 0;
        setAttempt(0);
        setNextRetryAt(null);
        setStatus('live');
        onOpenRef.current?.(isReconnect);
      };

      next.onmessage = (event: MessageEvent<unknown>) => {
        const message = parseStreamMessage(event.data);
        if (!message) return;
        setLastMessageAt(Date.now());
        // The greeting proves the server-side consumer accepted us (a status of
        // 'live' alone only means the handshake finished), so treat it as a
        // liveness signal and clear any accumulated retry backoff.
        if (message.type === 'connection.established') {
          attemptCount = 0;
          setAttempt(0);
        }
        onMessageRef.current(message);
      };

      // `onerror` is always followed by `onclose`; reconnection is handled there.
      next.onerror = () => undefined;

      next.onclose = (event: CloseEvent) => {
        if (disposed) return;
        socket = null;
        // A rejected token can never succeed on retry: surface the terminal
        // state instead of reconnecting until the attempts run out.
        if (TERMINAL_CLOSE_CODES.has(event.code)) {
          goOffline();
          return;
        }
        scheduleReconnect();
      };
    }

    reconnectRef.current = () => {
      clearRetryTimer();
      if (socket && socket.readyState <= WebSocket.OPEN) {
        socket.onclose = null;
        try {
          socket.close(1000, 'manual reconnect');
        } catch {
          /* already closing */
        }
      }
      socket = null;
      attemptCount = 0;
      setAttempt(0);
      connect();
    };

    connect();

    return () => {
      disposed = true;
      reconnectRef.current = null;
      clearRetryTimer();
      if (socket) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        try {
          socket.close(1000, 'unmounted');
        } catch {
          /* already closing */
        }
        socket = null;
      }
    };
  }, [token]);

  const reconnect = useCallback(() => {
    reconnectRef.current?.();
  }, []);

  return { status, attempt, nextRetryAt, lastMessageAt, reconnect };
}
