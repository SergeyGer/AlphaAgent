import { useCallback, useMemo, useRef, useState, type ReactElement } from 'react';
import { ApiError, api, errorMessage } from '../api/client';
import { useAuth } from '../hooks/useAuth';
import { useDecisionLogs } from '../hooks/useDecisionLogs';
import { usePortfolio } from '../hooks/usePortfolio';
import { usePortfolioStream } from '../hooks/usePortfolioStream';
import { useRecommendations } from '../hooks/useRecommendations';
import { useSnapshots } from '../hooks/useSnapshots';
import { useToasts } from '../hooks/useToasts';
import { useTransactions } from '../hooks/useTransactions';
import { feedItemFromLog, feedItemFromThinking, mergeFeed } from '../lib/feed';
import { formatCurrency, formatQuantity } from '../lib/format';
import type { FeedItem, PortfolioSnapshotEvent, SnapshotRange, StreamMessage } from '../types';
import { AllocationChart } from './AllocationChart';
import { ErrorState } from './Feedback';
import { EquityChart } from './EquityChart';
import { Header } from './Header';
import { MetricsGrid } from './MetricsGrid';
import { NewsSentimentPanel } from './NewsSentimentPanel';
import { RecommendationsPanel } from './RecommendationsPanel';
import { ThoughtsFeed } from './ThoughtsFeed';
import { ToastStack } from './ToastStack';
import { TradesTable } from './TradesTable';

/** Hard cap for the in-memory AI thoughts stream. */
const FEED_CAP = 100;
const TRANSACTION_LIMIT = 25;

/** Authenticated dashboard: REST state + live WebSocket overlay. */
export function Dashboard(): ReactElement {
  const { token, username, logout } = useAuth();
  const { toasts, push, dismiss } = useToasts();

  const portfolio = usePortfolio(token);
  const [range, setRange] = useState<SnapshotRange>('24H');
  const snapshots = useSnapshots(token, range);
  const transactions = useTransactions(token, TRANSACTION_LIMIT);
  const logs = useDecisionLogs(token);
  const recommendations = useRecommendations(token);

  const [liveFeed, setLiveFeed] = useState<FeedItem[]>([]);
  const [autonomyBusy, setAutonomyBusy] = useState(false);
  const [runAgentBusy, setRunAgentBusy] = useState(false);

  const {
    setData: setPortfolioData,
    refresh: refreshPortfolio,
    loading: portfolioLoading,
    error: portfolioError,
  } = portfolio;
  const { setData: setTransactionData, refresh: refreshTransactions } = transactions;
  const { upsert: upsertRecommendation, decide: decideRecommendation, refresh: refreshRecommendations } =
    recommendations;
  const { refresh: refreshSnapshots } = snapshots;
  const { refresh: refreshLogs } = logs;

  const refreshAll = useCallback(() => {
    refreshPortfolio();
    refreshSnapshots();
    refreshTransactions();
    refreshLogs();
    refreshRecommendations();
  }, [refreshPortfolio, refreshSnapshots, refreshTransactions, refreshLogs, refreshRecommendations]);

  // Latest refresh callback, reachable from the socket's `onOpen` handler.
  const refreshAllRef = useRef(refreshAll);
  refreshAllRef.current = refreshAll;

  const handleStreamMessage = useCallback(
    (message: StreamMessage) => {
      switch (message.type) {
        case 'portfolio.snapshot': {
          // A malformed or partial frame must never blank the dashboard, so
          // only keys that are actually present and non-null are merged; every
          // missing key keeps its previous value until the next REST resync.
          const snapshot: Partial<PortfolioSnapshotEvent> = message.payload;
          setPortfolioData((previous) => {
            if (!previous) return previous;
            const next = { ...previous };
            if (snapshot.metrics != null) next.metrics = snapshot.metrics;
            if (snapshot.assets != null) next.assets = snapshot.assets;
            return next;
          });
          break;
        }

        case 'trade.executed': {
          const transaction = message.payload;
          setTransactionData((previous) => {
            const list = previous ?? [];
            if (list.some((item) => item.id === transaction.id)) return list;
            return [transaction, ...list].slice(0, TRANSACTION_LIMIT);
          });
          push(
            'success',
            `${transaction.tx_type} ${formatQuantity(transaction.amount)} ${transaction.ticker} @ ${formatCurrency(
              transaction.price,
            )} · ${formatCurrency(transaction.gross_value_usd)}`,
          );
          break;
        }

        case 'decision.created': {
          const item = feedItemFromLog(message.payload);
          setLiveFeed((previous) => [item, ...previous.filter((row) => row.key !== item.key)].slice(0, FEED_CAP));
          break;
        }

        case 'agent.thinking': {
          const item = feedItemFromThinking(message.payload);
          setLiveFeed((previous) => [item, ...previous.filter((row) => row.key !== item.key)].slice(0, FEED_CAP));
          break;
        }

        case 'recommendation.created':
        case 'recommendation.updated': {
          upsertRecommendation(message.payload);
          break;
        }

        case 'autonomy.changed': {
          const next = message.payload.is_autonomous;
          setPortfolioData((previous) => (previous ? { ...previous, is_autonomous: next } : previous));
          push('info', `Autonomous trading ${next ? 'enabled' : 'disabled'}.`);
          break;
        }

        default:
          break;
      }
    },
    [push, setPortfolioData, setTransactionData, upsertRecommendation],
  );

  const handleStreamOpen = useCallback(
    (isReconnect: boolean) => {
      if (!isReconnect) return;
      refreshAllRef.current();
      push('info', 'Live stream restored — portfolio resynced.');
    },
    [push],
  );

  const stream = usePortfolioStream({
    token,
    onMessage: handleStreamMessage,
    onOpen: handleStreamOpen,
  });

  const handleToggleAutonomy = useCallback(
    async (next: boolean) => {
      setAutonomyBusy(true);
      try {
        const response = await api.toggleAutonomy(next);
        setPortfolioData((previous) =>
          previous ? { ...previous, is_autonomous: response.is_autonomous } : previous,
        );
        push(
          'success',
          response.message ||
            `Autonomous trading ${response.is_autonomous ? 'enabled' : 'disabled'}${
              response.changed ? '' : ' (no change)'
            }.`,
        );
      } finally {
        setAutonomyBusy(false);
      }
    },
    [push, setPortfolioData],
  );

  const handleRunAgent = useCallback(async () => {
    setRunAgentBusy(true);
    try {
      const response = await api.runAgent();
      const task = typeof response.task_id === 'string' ? response.task_id.slice(0, 8) : '';
      push('info', `Agent cycle queued${task ? ` · task ${task}` : ''}.`);
    } catch (error) {
      push('error', errorMessage(error));
    } finally {
      setRunAgentBusy(false);
    }
  }, [push]);

  const handleDecide = useCallback(
    async (id: number, decision: 'approve' | 'reject') => {
      try {
        const response = await decideRecommendation(id, decision);
        push(
          response.executed ? 'success' : 'info',
          response.message || `Recommendation ${response.status.toLowerCase()}.`,
        );
        if (response.executed) {
          refreshTransactions();
          refreshPortfolio();
        }
      } catch (error) {
        const conflict = error instanceof ApiError && error.isConflict;
        push(
          'error',
          conflict
            ? `Already decided — ${errorMessage(error)}`
            : `Could not ${decision} recommendation: ${errorMessage(error)}`,
        );
        refreshRecommendations();
      }
    },
    [decideRecommendation, push, refreshPortfolio, refreshRecommendations, refreshTransactions],
  );

  const baseFeed = useMemo(() => (logs.data ?? []).map(feedItemFromLog), [logs.data]);
  const feed = useMemo(() => mergeFeed(baseFeed, liveFeed, FEED_CAP), [baseFeed, liveFeed]);

  const refreshing =
    portfolioLoading || snapshots.loading || transactions.loading || logs.loading || recommendations.loading;

  return (
    <div className="min-h-screen bg-slate-950">
      <Header
        username={username ?? portfolio.data?.username ?? null}
        riskProfile={
          portfolio.data?.risk_profile_display ?? portfolio.data?.risk_profile ?? null
        }
        connection={{
          status: stream.status,
          attempt: stream.attempt,
          nextRetryAt: stream.nextRetryAt,
          lastMessageAt: stream.lastMessageAt,
          reconnect: stream.reconnect,
        }}
        isAutonomous={portfolio.data?.is_autonomous ?? false}
        autonomyBusy={autonomyBusy}
        autonomyDisabled={portfolio.data === null}
        onToggleAutonomy={handleToggleAutonomy}
        onRunAgent={() => {
          void handleRunAgent();
        }}
        runAgentBusy={runAgentBusy}
        onRefresh={refreshAll}
        refreshing={refreshing}
        onLogout={logout}
      />

      <main className="mx-auto max-w-[1800px] space-y-4 px-3 py-4 sm:px-4">
        {portfolioError && portfolio.data === null ? (
          <ErrorState
            message={`Could not load your portfolio — ${portfolioError}`}
            onRetry={refreshPortfolio}
          />
        ) : null}

        <MetricsGrid portfolio={portfolio.data} loading={portfolioLoading} />

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <div className="flex min-w-0 lg:col-span-2">
            <EquityChart
              className="h-full w-full"
              snapshots={snapshots.data ?? []}
              range={range}
              onRangeChange={setRange}
              loading={snapshots.loading}
              error={snapshots.error}
              onRetry={refreshSnapshots}
            />
          </div>

          <div className="flex min-w-0">
            <AllocationChart
              className="h-full w-full"
              portfolio={portfolio.data}
              loading={portfolioLoading}
              error={portfolioError}
              onRetry={refreshPortfolio}
            />
          </div>

          <div className="flex min-w-0 lg:col-span-2">
            <ThoughtsFeed
              className="h-full w-full"
              items={feed}
              loading={logs.loading}
              error={logs.error}
              onRetry={refreshLogs}
              cap={FEED_CAP}
            />
          </div>

          <div className="flex min-w-0">
            <RecommendationsPanel
              className="h-full w-full"
              pending={recommendations.pending}
              history={recommendations.history}
              loading={recommendations.loading}
              error={recommendations.error}
              busyIds={recommendations.busyIds}
              onDecide={(id, decision) => {
                void handleDecide(id, decision);
              }}
              onRetry={refreshRecommendations}
            />
          </div>

          <div className="flex min-w-0 lg:col-span-3">
            <NewsSentimentPanel className="h-full w-full" />
          </div>

          <div className="flex min-w-0 lg:col-span-3">
            <TradesTable
              className="h-full w-full"
              transactions={transactions.data ?? []}
              loading={transactions.loading}
              error={transactions.error}
              onRetry={refreshTransactions}
            />
          </div>
        </div>
      </main>

      <ToastStack toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
