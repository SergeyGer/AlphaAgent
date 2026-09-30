import { useEffect, useRef, type ReactElement, type ReactNode } from 'react';
import { cn } from '../lib/cn';
import { Spinner } from './Feedback';

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: 'primary' | 'danger';
  busy?: boolean;
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

/** Accessible confirmation modal (Escape to cancel, focus moved to confirm). */
export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  tone = 'primary',
  busy = false,
  error = null,
  onConfirm,
  onCancel,
}: ConfirmDialogProps): ReactElement | null {
  const confirmRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!open) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busy) onCancel();
    };

    document.addEventListener('keydown', onKeyDown);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    confirmRef.current?.focus();

    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [open, busy, onCancel]);

  if (!open) return null;

  const confirmClass =
    tone === 'danger'
      ? 'bg-rose-500 text-slate-950 hover:bg-rose-400 focus-visible:ring-rose-300'
      : 'bg-emerald-500 text-slate-950 hover:bg-emerald-400 focus-visible:ring-emerald-300';

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-dialog-title"
    >
      <button
        type="button"
        aria-label="Close dialog"
        tabIndex={-1}
        onClick={() => {
          if (!busy) onCancel();
        }}
        className="absolute inset-0 h-full w-full cursor-default bg-slate-950/80 backdrop-blur-sm"
      />
      <div className="relative z-10 w-full max-w-md animate-fade-in rounded-xl border border-slate-700/80 bg-slate-900 p-5 shadow-panel">
        <h2 id="confirm-dialog-title" className="text-sm font-semibold text-slate-100">
          {title}
        </h2>
        {description ? <div className="mt-2 text-xs leading-relaxed text-slate-400">{description}</div> : null}
        {error ? (
          <p role="alert" className="mt-3 rounded border border-rose-500/30 bg-rose-500/10 px-2 py-1.5 text-xs text-rose-200">
            {error}
          </p>
        ) : null}
        <div className="mt-5 flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="rounded-md border border-slate-700 px-3 py-1.5 text-xs font-medium text-slate-300 transition-colors hover:bg-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-slate-500 disabled:opacity-50"
          >
            {cancelLabel}
          </button>
          <button
            ref={confirmRef}
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className={cn(
              'inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-xs font-semibold transition-colors focus:outline-none focus-visible:ring-2 disabled:opacity-60',
              confirmClass,
            )}
          >
            {busy ? <Spinner className="border-slate-900/40 border-t-slate-900" /> : null}
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
