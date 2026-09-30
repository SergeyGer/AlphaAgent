import type { ReactElement } from 'react';
import { cn } from '../lib/cn';
import type { Toast } from '../types';

const TONE: Record<Toast['kind'], string> = {
  success: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-100',
  error: 'border-rose-500/40 bg-rose-500/10 text-rose-100',
  info: 'border-slate-600/50 bg-slate-800/80 text-slate-100',
};

const ICON: Record<Toast['kind'], string> = {
  success: '✓',
  error: '!',
  info: 'i',
};

export interface ToastStackProps {
  toasts: Toast[];
  onDismiss: (id: number) => void;
}

export function ToastStack({ toasts, onDismiss }: ToastStackProps): ReactElement | null {
  if (toasts.length === 0) return null;

  return (
    <div
      className="pointer-events-none fixed bottom-4 right-4 z-40 flex w-[min(22rem,calc(100vw-2rem))] flex-col gap-2"
      role="status"
      aria-live="polite"
    >
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={cn(
            'pointer-events-auto flex animate-fade-in items-start gap-2 rounded-lg border px-3 py-2 shadow-panel backdrop-blur-sm',
            TONE[toast.kind],
          )}
        >
          <span aria-hidden="true" className="mt-px font-mono text-[11px] opacity-80">
            {ICON[toast.kind]}
          </span>
          <p className="flex-1 text-xs leading-relaxed">{toast.message}</p>
          <button
            type="button"
            aria-label="Dismiss notification"
            onClick={() => onDismiss(toast.id)}
            className="rounded px-1 font-mono text-[11px] opacity-60 transition-opacity hover:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-slate-400"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
