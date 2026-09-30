import type { ReactElement, ReactNode } from 'react';
import { cn } from '../lib/cn';

export interface PanelProps {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}

/** Bordered dark panel used for every dashboard module. */
export function Panel({
  title,
  subtitle,
  actions,
  children,
  className,
  bodyClassName,
}: PanelProps): ReactElement {
  const hasHeader = Boolean(title) || Boolean(actions) || Boolean(subtitle);

  return (
    <section
      className={cn(
        'flex min-w-0 flex-col rounded-xl border border-slate-800/80 bg-slate-900/50 shadow-panel backdrop-blur-sm transition-colors duration-300 hover:border-slate-700/80',
        className,
      )}
    >
      {hasHeader ? (
        <header className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 border-b border-slate-800/70 px-4 py-3">
          <div className="min-w-0">
            {typeof title === 'string' ? (
              <h2 className="truncate text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-400">
                {title}
              </h2>
            ) : (
              title
            )}
            {subtitle ? <p className="mt-0.5 text-xs text-slate-500">{subtitle}</p> : null}
          </div>
          {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
        </header>
      ) : null}
      <div className={cn('min-w-0 flex-1 p-4', bodyClassName)}>{children}</div>
    </section>
  );
}
