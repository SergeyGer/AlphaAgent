import { useState, type ReactElement } from 'react';
import { errorMessage } from '../api/client';
import { cn } from '../lib/cn';
import { ConfirmDialog } from './ConfirmDialog';

export interface AutonomyToggleProps {
  isAutonomous: boolean;
  /** True while the toggle request is in flight. */
  busy?: boolean;
  disabled?: boolean;
  /** Resolves on success; throws to keep the dialog open with an error. */
  onChange: (next: boolean) => Promise<void>;
}

/**
 * Autonomy switch. Enabling requires an explicit confirmation because it lets
 * the agent place trades without human approval; disabling is immediate.
 */
export function AutonomyToggle({
  isAutonomous,
  busy = false,
  disabled = false,
  onChange,
}: AutonomyToggleProps): ReactElement {
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const apply = async (next: boolean): Promise<void> => {
    setSubmitting(true);
    setError(null);
    try {
      await onChange(next);
      setConfirmOpen(false);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setSubmitting(false);
    }
  };

  const handleClick = () => {
    if (disabled || busy) return;
    if (isAutonomous) {
      void apply(false);
      return;
    }
    setError(null);
    setConfirmOpen(true);
  };

  const isBusy = busy || submitting;

  return (
    <>
      <button
        type="button"
        role="switch"
        aria-checked={isAutonomous}
        aria-label="Toggle autonomous trading"
        disabled={disabled || isBusy}
        onClick={handleClick}
        title={
          isAutonomous
            ? 'Autonomous trading is ON — the agent trades without approval'
            : 'Autonomous trading is OFF — every trade needs your approval'
        }
        className={cn(
          'group inline-flex items-center gap-2 rounded-md border px-2 py-1 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400/60 disabled:cursor-not-allowed disabled:opacity-60',
          isAutonomous
            ? 'border-emerald-500/40 bg-emerald-500/10 hover:bg-emerald-500/15'
            : 'border-slate-700 bg-slate-950/60 hover:bg-slate-800/60',
        )}
      >
        <span
          className={cn(
            'relative h-3.5 w-7 shrink-0 rounded-full transition-colors duration-300',
            isAutonomous ? 'bg-emerald-500/70' : 'bg-slate-600',
          )}
        >
          <span
            className={cn(
              'absolute top-0.5 h-2.5 w-2.5 rounded-full bg-slate-950 transition-all duration-300 ease-terminal',
              isAutonomous ? 'left-[1.05rem]' : 'left-0.5',
            )}
          />
        </span>
        <span
          className={cn(
            'font-mono text-[10px] font-semibold uppercase tracking-[0.14em]',
            isAutonomous ? 'text-emerald-300' : 'text-slate-400',
          )}
        >
          {isBusy ? 'SYNCING…' : isAutonomous ? 'AUTONOMOUS' : 'MANUAL'}
        </span>
      </button>

      <ConfirmDialog
        open={confirmOpen}
        title="Enable autonomous trading?"
        tone="danger"
        confirmLabel="Enable autonomy"
        cancelLabel="Keep manual"
        busy={submitting}
        error={error}
        onCancel={() => {
          if (!submitting) setConfirmOpen(false);
        }}
        onConfirm={() => {
          void apply(true);
        }}
        description={
          <div className="space-y-2">
            <p>
              AlphaAgent will be able to <strong className="text-slate-200">execute trades without your
              approval</strong>, within your configured risk limits.
            </p>
            <ul className="list-inside list-disc space-y-1 text-slate-500">
              <li>Trades are still capped by your max trade budget and daily loss limit.</li>
              <li>You can switch back to manual mode at any time.</li>
            </ul>
          </div>
        }
      />
    </>
  );
}
