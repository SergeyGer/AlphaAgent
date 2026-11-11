import { useCallback, useEffect, useState } from 'react';
import {
  isReplayTimingMode,
  type ReplayTimingMode,
} from '../lib/decisionRuns';

/**
 * Cursor + timing mode as navigation state.
 *
 * A reviewer who seeks to sequence 41, switches to recorded-time playback and
 * then presses browser Back must return to the previous view, not to the run
 * list or to a fresh replay. Both values therefore live in the hash query:
 *
 *   `#/runs/<id>?at=41&timing=recorded`
 *
 * `at=start` is explicit so "at the beginning" is distinguishable from "no
 * cursor has been chosen yet". The latter is what lets a run open at its tail
 * once, then stay put as new events arrive.
 */

export interface ReplayLocation {
  /** `undefined` means no cursor has been chosen yet. `null` means start. */
  cursorSeq: number | null | undefined;
  timing: ReplayTimingMode;
}

export function parseReplayLocation(hash: string): ReplayLocation {
  const raw = (hash ?? '').replace(/^#/, '');
  const queryStart = raw.indexOf('?');
  const query = queryStart === -1 ? '' : raw.slice(queryStart + 1);
  const params = new URLSearchParams(query);

  const at = params.get('at');
  let cursorSeq: number | null | undefined;
  if (at === null) {
    cursorSeq = undefined;
  } else if (at.trim().toLowerCase() === 'start') {
    cursorSeq = null;
  } else if (/^\d+$/.test(at.trim())) {
    cursorSeq = Number.parseInt(at.trim(), 10);
  } else {
    cursorSeq = undefined;
  }

  const timing = params.get('timing');
  return {
    cursorSeq,
    timing: isReplayTimingMode(timing) ? timing : 'compressed',
  };
}

export function buildReplayHash(
  runId: string,
  cursorSeq: number | null,
  timing: ReplayTimingMode,
): string {
  const at = cursorSeq === null ? 'start' : String(cursorSeq);
  return `#/runs/${encodeURIComponent(runId)}?at=${encodeURIComponent(at)}&timing=${encodeURIComponent(timing)}`;
}

export interface ReplayNavigationState extends ReplayLocation {
  /** False until an explicit cursor is in the URL (or the caller sets one). */
  hasCursor: boolean;
  setCursor: (seq: number | null, options?: { history?: 'push' | 'replace' }) => void;
  setTiming: (mode: ReplayTimingMode) => void;
}

export function useReplayNavigation(runId: string): ReplayNavigationState {
  const [location, setLocation] = useState<ReplayLocation>(() =>
    parseReplayLocation(typeof window === 'undefined' ? '' : window.location.hash),
  );

  useEffect(() => {
    const sync = (): void => setLocation(parseReplayLocation(window.location.hash));
    window.addEventListener('hashchange', sync);
    window.addEventListener('popstate', sync);
    return () => {
      window.removeEventListener('hashchange', sync);
      window.removeEventListener('popstate', sync);
    };
  }, []);

  const write = useCallback(
    (cursorSeq: number | null, timing: ReplayTimingMode, history: 'push' | 'replace') => {
      const hash = buildReplayHash(runId, cursorSeq, timing);
      const url = `${window.location.pathname}${window.location.search}${hash}`;
      if (history === 'replace') window.history.replaceState(null, '', url);
      else window.history.pushState(null, '', url);
      setLocation({ cursorSeq, timing });
    },
    [runId],
  );

  const setCursor = useCallback(
    (seq: number | null, options: { history?: 'push' | 'replace' } = {}) => {
      write(seq, location.timing, options.history ?? 'push');
    },
    [location.timing, write],
  );

  const setTiming = useCallback(
    (mode: ReplayTimingMode) => {
      if (location.cursorSeq === undefined) {
        // Preserve "no explicit cursor yet"; the run view initialises it once
        // the events arrive. Writing `at=start` here would yank the reader to
        // the beginning merely for choosing a timing mode.
        const hash = `#/runs/${encodeURIComponent(runId)}?timing=${encodeURIComponent(mode)}`;
        const url = `${window.location.pathname}${window.location.search}${hash}`;
        window.history.pushState(null, '', url);
        setLocation({ cursorSeq: undefined, timing: mode });
        return;
      }
      write(location.cursorSeq, mode, 'push');
    },
    [location.cursorSeq, runId, write],
  );

  return {
    ...location,
    hasCursor: location.cursorSeq !== undefined,
    setCursor,
    setTiming,
  };
}
