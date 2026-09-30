import type { ReactElement } from 'react';
import { cn } from '../lib/cn';
import type { ConnectionStatus } from '../types';
import { AutonomyToggle } from './AutonomyToggle';
import { ConnectionIndicator } from './ConnectionIndicator';

export interface HeaderProps {
  username: string | null;
  riskProfile: string | null;
  connection: {
    status: ConnectionStatus;
    attempt: number;
    nextRetryAt: number | null;
    lastMessageAt: number | null;
    reconnect: () => void;
  };
  isAutonomous: boolean;
  autonomyBusy: boolean;
  autonomyDisabled: boolean;
  onToggleAutonomy: (next: boolean) => Promise<void>;
  onRunAgent: () => void;
  runAgentBusy: boolean;
  onRefresh: () => void;
  refreshing: boolean;
  onLogout: () => void;
}

function IconButton({
  label,
  onClick,
  disabled,
  spinning = false,
  children,
  title,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  spinning?: boolean;
  children: ReactElement;
  title?: string;
}): ReactElement {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title ?? label}
      aria-label={label}
      className="inline-flex items-center gap-1.5 rounded-md border border-slate-700 bg-slate-950/60 px-2 py-1 font-mono text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-300 transition-colors hover:bg-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400/60 disabled:cursor-not-allowed disabled:opacity-50"
    >
      <span className={cn('inline-flex h-3 w-3 items-center justify-center', spinning && 'animate-spin')}>
        {children}
      </span>
      <span className="hidden sm:inline">{label}</span>
    </button>
  );
}

const RefreshIcon = (): ReactElement => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="h-3 w-3">
    <path d="M21 12a9 9 0 1 1-3-6.7" strokeLinecap="round" />
    <path d="M21 3v6h-6" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

const BoltIcon = (): ReactElement => (
  <svg viewBox="0 0 24 24" fill="currentColor" className="h-3 w-3">
    <path d="M13 2 4.5 13.5H11l-1 8.5 8.5-11.5H12l1-8.5Z" />
  </svg>
);

const LogoutIcon = (): ReactElement => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="h-3 w-3">
    <path d="M15 17l5-5-5-5" strokeLinecap="round" strokeLinejoin="round" />
    <path d="M20 12H9" strokeLinecap="round" />
    <path d="M12 19H6a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h6" strokeLinecap="round" />
  </svg>
);

export function Header({
  username,
  riskProfile,
  connection,
  isAutonomous,
  autonomyBusy,
  autonomyDisabled,
  onToggleAutonomy,
  onRunAgent,
  runAgentBusy,
  onRefresh,
  refreshing,
  onLogout,
}: HeaderProps): ReactElement {
  return (
    <header className="sticky top-0 z-30 border-b border-slate-800/80 bg-slate-950/85 backdrop-blur-md">
      <div className="mx-auto flex max-w-[1800px] flex-wrap items-center gap-x-3 gap-y-2 px-3 py-2.5 sm:px-4">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-emerald-400 to-sky-500 font-mono text-base font-bold text-slate-950">
            α
          </span>
          <div className="min-w-0">
            <h1 className="truncate text-sm font-semibold tracking-tight text-slate-100">AlphaAgent</h1>
            <p className="hidden truncate font-mono text-[10px] uppercase tracking-[0.18em] text-slate-500 sm:block">
              Autonomous Investment Terminal
            </p>
          </div>
        </div>

        <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
          <ConnectionIndicator
            status={connection.status}
            attempt={connection.attempt}
            nextRetryAt={connection.nextRetryAt}
            lastMessageAt={connection.lastMessageAt}
            onReconnect={connection.reconnect}
          />

          <IconButton
            label={runAgentBusy ? 'Queueing' : 'Run agent'}
            onClick={onRunAgent}
            disabled={runAgentBusy}
            title="Queue an immediate AI analysis cycle (POST /api/portfolio/run-agent/)"
          >
            <BoltIcon />
          </IconButton>

          <IconButton
            label="Refresh"
            onClick={onRefresh}
            disabled={refreshing}
            spinning={refreshing}
            title="Refetch all REST resources"
          >
            <RefreshIcon />
          </IconButton>

          <AutonomyToggle
            isAutonomous={isAutonomous}
            busy={autonomyBusy}
            disabled={autonomyDisabled}
            onChange={onToggleAutonomy}
          />

          <div className="flex items-center gap-2 rounded-md border border-slate-800 bg-slate-950/60 px-2 py-1">
            <span className="flex h-4 w-4 items-center justify-center rounded-full bg-slate-700 font-mono text-[9px] font-bold uppercase text-slate-200">
              {(username ?? '?').slice(0, 1)}
            </span>
            <span className="max-w-[8rem] truncate font-mono text-[11px] text-slate-300">
              {username ?? 'unknown'}
            </span>
            {riskProfile ? (
              <span className="hidden font-mono text-[10px] uppercase tracking-wider text-slate-500 md:inline">
                · {riskProfile}
              </span>
            ) : null}
          </div>

          <IconButton label="Logout" onClick={onLogout} title="Sign out and clear the stored token">
            <LogoutIcon />
          </IconButton>
        </div>
      </div>
    </header>
  );
}
