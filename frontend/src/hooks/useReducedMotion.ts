import { useEffect, useState } from 'react';

/**
 * `prefers-reduced-motion: reduce`, as a reactive boolean.
 *
 * The stylesheet already collapses CSS animations and transitions. Playback is
 * still a sequence of state changes rather than an animation, so the control
 * keeps working; this hook lets the replay surface say plainly that no animated
 * motion is being produced and avoids adding any decorative transition on top.
 */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState<boolean>(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false;
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  });

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return undefined;
    const query = window.matchMedia('(prefers-reduced-motion: reduce)');
    const handleChange = (): void => setReduced(query.matches);
    handleChange();
    if (typeof query.addEventListener === 'function') {
      query.addEventListener('change', handleChange);
      return () => query.removeEventListener('change', handleChange);
    }
    // Safari < 14 had only the deprecated listener API.
    query.addListener(handleChange);
    return () => query.removeListener(handleChange);
  }, []);

  return reduced;
}
