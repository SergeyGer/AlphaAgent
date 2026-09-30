import { useEffect, useState } from 'react';

/**
 * Re-renders on an interval so relative timestamps ("12s ago") stay fresh.
 * Defaults to a 10s tick; pauses while the tab is hidden.
 */
export function useNow(intervalMs = 10_000): number {
  const [now, setNow] = useState<number>(() => Date.now());

  useEffect(() => {
    let timer: number | null = null;

    const tick = () => setNow(Date.now());

    const start = () => {
      if (timer !== null) return;
      timer = window.setInterval(tick, intervalMs);
    };

    const stop = () => {
      if (timer === null) return;
      window.clearInterval(timer);
      timer = null;
    };

    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        tick();
        start();
      } else {
        stop();
      }
    };

    if (document.visibilityState === 'visible') start();
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      stop();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [intervalMs]);

  return now;
}
