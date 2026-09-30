import { useMemo, type ReactElement } from 'react';
import { formatCurrency, formatQuantity, formatRelativeTime } from '../lib/format';
import { useNow } from '../hooks/useNow';
import type { Transaction } from '../types';
import { ExecutorBadge, SideBadge } from './Badge';
import { EmptyState, ErrorState, SkeletonRows } from './Feedback';
import { Panel } from './Panel';

export interface TradesTableProps {
  transactions: Transaction[];
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  className?: string;
}

const HEAD_CELL =
  'px-3 py-2 text-left font-mono text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500';
const BODY_CELL = 'px-3 py-2 font-mono text-xs tabular-nums text-slate-300';

/** Recent executions — newest first. */
export function TradesTable({
  transactions,
  loading,
  error,
  onRetry,
  className,
}: TradesTableProps): ReactElement {
  const now = useNow(15_000);

  const rows = useMemo(() => {
    return transactions
      .slice()
      .sort((a, b) => {
        const left = Date.parse(a.timestamp);
        const right = Date.parse(b.timestamp);
        if (Number.isNaN(left) || Number.isNaN(right)) return b.id - a.id;
        return right - left;
      });
  }, [transactions]);

  return (
    <Panel
      title="Recent Trades"
      subtitle={rows.length > 0 ? `${rows.length} most recent executions` : 'Executions by the agent and by you'}
      bodyClassName="p-0"
      className={className}
    >
      {error ? (
        <div className="p-4">
          <ErrorState message={error} onRetry={onRetry} />
        </div>
      ) : loading && rows.length === 0 ? (
        <div className="p-4">
          <SkeletonRows rows={4} />
        </div>
      ) : rows.length === 0 ? (
        <div className="p-4">
          <EmptyState
            title="No trades yet"
            description="Filled orders show up here, streamed live over the WebSocket."
          />
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[46rem] border-collapse">
            <thead className="bg-slate-950/60">
              <tr className="border-b border-slate-800">
                <th scope="col" className={HEAD_CELL}>
                  Time
                </th>
                <th scope="col" className={HEAD_CELL}>
                  Ticker
                </th>
                <th scope="col" className={HEAD_CELL}>
                  Side
                </th>
                <th scope="col" className={`${HEAD_CELL} text-right`}>
                  Amount
                </th>
                <th scope="col" className={`${HEAD_CELL} text-right`}>
                  Price
                </th>
                <th scope="col" className={`${HEAD_CELL} text-right`}>
                  Notional
                </th>
                <th scope="col" className={`${HEAD_CELL} text-right`}>
                  Executor
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((transaction) => (
                <tr
                  key={transaction.id}
                  className="border-b border-slate-800/50 transition-colors duration-200 last:border-0 hover:bg-slate-800/30"
                >
                  <td className={BODY_CELL} title={new Date(transaction.timestamp).toString()}>
                    <span className="text-slate-400">{formatRelativeTime(transaction.timestamp, now)}</span>
                  </td>
                  <td className={`${BODY_CELL} font-semibold text-slate-100`}>{transaction.ticker}</td>
                  <td className={BODY_CELL}>
                    <SideBadge side={transaction.tx_type} />
                  </td>
                  <td className={`${BODY_CELL} text-right`}>{formatQuantity(transaction.amount)}</td>
                  <td className={`${BODY_CELL} text-right`}>{formatCurrency(transaction.price)}</td>
                  <td className={`${BODY_CELL} text-right text-slate-100`}>
                    {formatCurrency(transaction.gross_value_usd)}
                  </td>
                  <td className={`${BODY_CELL} text-right`}>
                    <span className="inline-flex justify-end">
                      <ExecutorBadge executor={transaction.executed_by} />
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
