import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import {
  buildReplayHash,
  parseReplayLocation,
  useReplayNavigation,
} from './useReplayNavigation';

/**
 * FIX-04 — cursor and timing mode survive navigation.
 *
 * The hash query is the source of truth. `at=start` must remain distinct from
 * "no cursor chosen yet", because the latter is what lets a stored run open at
 * its tail exactly once.
 */

afterEach(() => {
  window.history.replaceState(null, '', '/');
});

describe('parseReplayLocation', () => {
  it('distinguishes an absent cursor from the start cursor', () => {
    expect(parseReplayLocation('#/runs/run-1')).toEqual({
      cursorSeq: undefined,
      timing: 'compressed',
    });
    expect(parseReplayLocation('#/runs/run-1?at=start')).toEqual({
      cursorSeq: null,
      timing: 'compressed',
    });
    expect(parseReplayLocation('#/runs/run-1?at=41&timing=recorded')).toEqual({
      cursorSeq: 41,
      timing: 'recorded',
    });
  });

  it('falls back to compressed for an unknown or malformed timing mode', () => {
    expect(parseReplayLocation('#/runs/run-1?timing=warp').timing).toBe('compressed');
    expect(parseReplayLocation('#/runs/run-1?at=-2&timing=recorded')).toEqual({
      cursorSeq: undefined,
      timing: 'recorded',
    });
  });

  it('round-trips through the hash builder', () => {
    expect(parseReplayLocation(buildReplayHash('run/1', 9, 'recorded'))).toEqual({
      cursorSeq: 9,
      timing: 'recorded',
    });
    expect(parseReplayLocation(buildReplayHash('run-1', null, 'compressed'))).toEqual({
      cursorSeq: null,
      timing: 'compressed',
    });
  });
});

describe('useReplayNavigation', () => {
  it('writes a pushed cursor and keeps the timing mode', () => {
    window.history.replaceState(null, '', '#/runs/run-1?at=5&timing=recorded');
    const { result } = renderHook(() => useReplayNavigation('run-1'));

    expect(result.current.cursorSeq).toBe(5);
    expect(result.current.timing).toBe('recorded');

    act(() => result.current.setCursor(6, { history: 'push' }));
    expect(result.current.cursorSeq).toBe(6);
    expect(window.location.hash).toContain('at=6');
    expect(window.location.hash).toContain('timing=recorded');
  });

  it('syncs from browser back/forward traversal', () => {
    window.history.replaceState(null, '', '#/runs/run-1?at=5&timing=compressed');
    const { result } = renderHook(() => useReplayNavigation('run-1'));
    expect(result.current.cursorSeq).toBe(5);

    // A traversal changes the URL before firing popstate/hashchange.
    window.history.replaceState(null, '', '#/runs/run-1?at=2&timing=recorded');
    act(() => {
      window.dispatchEvent(new Event('popstate'));
    });

    expect(result.current.cursorSeq).toBe(2);
    expect(result.current.timing).toBe('recorded');
  });

  it('preserves "no explicit cursor" when only the timing mode changes', () => {
    window.history.replaceState(null, '', '#/runs/run-1');
    const { result } = renderHook(() => useReplayNavigation('run-1'));
    expect(result.current.hasCursor).toBe(false);

    act(() => result.current.setTiming('recorded'));
    expect(result.current.hasCursor).toBe(false);
    expect(result.current.timing).toBe('recorded');
    expect(window.location.hash).not.toContain('at=');
  });
});
