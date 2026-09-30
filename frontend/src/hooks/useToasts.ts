import { useCallback, useRef, useState } from 'react';
import type { Toast } from '../types';

let toastSequence = 0;

export interface UseToastsResult {
  toasts: Toast[];
  push: (kind: Toast['kind'], message: string) => void;
  dismiss: (id: number) => void;
}

/** Minimal toast queue used for trade alerts and API errors. */
export function useToasts(): UseToastsResult {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const timers = useRef(new Map<number, number>());

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((toast) => toast.id !== id));
    const timer = timers.current.get(id);
    if (timer !== undefined) {
      window.clearTimeout(timer);
      timers.current.delete(id);
    }
  }, []);

  const push = useCallback(
    (kind: Toast['kind'], message: string) => {
      toastSequence += 1;
      const id = toastSequence;
      setToasts((current) => [...current.slice(-4), { id, kind, message }]);
      const timer = window.setTimeout(() => {
        timers.current.delete(id);
        setToasts((current) => current.filter((toast) => toast.id !== id));
      }, kind === 'error' ? 8_000 : 5_000);
      timers.current.set(id, timer);
    },
    [],
  );

  return { toasts, push, dismiss };
}
