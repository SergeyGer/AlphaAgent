import { useEffect, useRef, useState } from 'react';

export type FlashDirection = 'up' | 'down';

/**
 * Detects numeric changes (including WebSocket pushes) and returns a short
 * lived direction flag used to flash the value green/red.
 */
export function useValueFlash(value: number | null, durationMs = 900): FlashDirection | null {
  const previousRef = useRef<number | null>(value);
  const [flash, setFlash] = useState<FlashDirection | null>(null);

  useEffect(() => {
    const previous = previousRef.current;
    previousRef.current = value;

    if (previous === null || value === null || previous === value) return;

    setFlash(value > previous ? 'up' : 'down');
    const timer = window.setTimeout(() => setFlash(null), durationMs);
    return () => window.clearTimeout(timer);
  }, [value, durationMs]);

  return flash;
}

/** Tailwind classes for a flash direction. */
export function flashClass(flash: FlashDirection | null): string {
  if (flash === 'up') return 'animate-flash-up';
  if (flash === 'down') return 'animate-flash-down';
  return '';
}
