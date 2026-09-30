import type { ReactElement, ReactNode } from 'react';
import { cn } from '../lib/cn';

export function Spinner({ className }: { className?: string }): ReactElement {
  return (
    <span
      role="status"
      aria-label="Loading"
      className={cn(
        'inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-slate-600 border-t-emerald-400',
        className,
      )}
    />
  );
}

export function Skeleton({ className }: { className?: string }): ReactElement {
  return (
    <span
      aria-hidden="true"
      className={cn('block overflow-hidden rounded bg-slate-800/70', className)}
    />
  );
}

export function SkeletonRows({ rows = 3, className }: { rows?: number; className?: string }): ReactElement {
  return (
    <div className={cn('space-y-2', className)}>
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} className="h-10 w-full" />
      ))}
    </div>
  );
}

export interface EmptyStateProps {
  title: string;
  description?: ReactNode;
  icon?: ReactNode;
  className?: string;
  compact?: boolean;
}

export function EmptyState({ title, description, icon, className, compact = false }: EmptyStateProps): ReactElement {
  return (
    <div
      className={cn(
        'flex flex-col items-center justify-center gap-1.5 rounded-lg border border-dashed border-slate-800 bg-slate-950/40 text-center',
        compact ? 'px-4 py-6' : 'px-6 py-10',
        className,
      )}
    >
      {icon ? <div className="text-slate-600">{icon}</div> : null}
      <p className="font-mono text-xs uppercase tracking-wider text-slate-400">{title}</p>
      {description ? <p className="max-w-sm text-xs text-slate-500">{description}</p> : null}
    </div>
  );
}

export interface ErrorStateProps {
  message: string;
  onRetry?: () => void;
  className?: string;
  compact?: boolean;
}

export function ErrorState({ message, onRetry, className, compact = false }: ErrorStateProps): ReactElement {
  return (
    <div
      role="alert"
      className={cn(
        'flex flex-col items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/5',
        compact ? 'px-3 py-2.5' : 'px-4 py-4',
        className,
      )}
    >
      <div className="flex items-start gap-2">
        <span aria-hidden="true" className="mt-px font-mono text-xs text-rose-400">
          !
        </span>
        <p className="text-xs text-rose-200">{message}</p>
      </div>
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="rounded border border-rose-500/40 px-2 py-1 font-mono text-[10px] uppercase tracking-wider text-rose-200 transition-colors hover:bg-rose-500/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-rose-400/60"
        >
          Retry
        </button>
      ) : null}
    </div>
  );
}
