import { useCallback, useState, type FormEvent, type ReactElement } from 'react';
import { cn } from '../lib/cn';
import { useAuth } from '../hooks/useAuth';
import { useMarketNews, NEWS_LIMIT } from '../hooks/useMarketNews';
import { useNow } from '../hooks/useNow';
import { formatQuantity, formatRelativeTime, formatSignedFixed, toNumber } from '../lib/format';
import type { MarketNewsReport, NewsArticle, NewsSentimentSummary } from '../types';
import { Badge } from './Badge';
import { EmptyState, ErrorState, SkeletonRows, Spinner } from './Feedback';
import { Panel } from './Panel';

/** Watchlist offered as one-click presets next to the free-text field. */
export const NEWS_TICKERS: readonly string[] = ['AAPL', 'TSLA', 'BTC'];

const DEFAULT_TICKER = 'AAPL';

export interface NewsSentimentPanelProps {
  className?: string;
}

function polarityTone(polarity: number): 'emerald' | 'rose' | 'slate' {
  if (polarity > 0) return 'emerald';
  if (polarity < 0) return 'rose';
  return 'slate';
}

/** Signed polarity badge, e.g. `+3.00` / `-2.50`. */
function PolarityBadge({ polarity }: { polarity: number }): ReactElement {
  return <Badge tone={polarityTone(polarity)}>{formatSignedFixed(polarity)}</Badge>;
}

function ArticleRow({ article, now }: { article: NewsArticle; now: number }): ReactElement {
  const linkable = typeof article.url === 'string' && article.url.trim() !== '';
  const titleClass = 'min-w-0 flex-1 text-[11px] font-medium leading-snug text-slate-200 transition-colors';

  return (
    <li className="rounded-md border border-slate-800/60 bg-slate-950/40 p-2 transition-colors duration-300 hover:border-slate-700/80 hover:bg-slate-900/40">
      <div className="flex items-start gap-2">
        {linkable ? (
          <a
            href={article.url}
            target="_blank"
            rel="noopener noreferrer"
            title={article.title}
            className={cn(titleClass, 'hover:text-emerald-300 hover:underline')}
          >
            {article.title}
          </a>
        ) : (
          <span title={article.title} className={titleClass}>
            {article.title}
          </span>
        )}
        <PolarityBadge polarity={article.polarity} />
      </div>

      <div className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 font-mono text-[10px] text-slate-500">
        <span className="truncate">{article.source || 'Unknown source'}</span>
        <span aria-hidden="true">·</span>
        <span className="tabular-nums">{formatRelativeTime(article.published_at, now)}</span>
      </div>
    </li>
  );
}

function CoverageColumn({
  title,
  tone,
  articles,
  now,
  emptyLabel,
}: {
  title: string;
  tone: 'emerald' | 'rose';
  articles: NewsArticle[];
  now: number;
  emptyLabel: string;
}): ReactElement {
  return (
    <section
      aria-label={title}
      className={cn(
        'min-w-0 rounded-lg border-l-2 bg-slate-950/30 py-2 pl-2.5 pr-2',
        tone === 'emerald' ? 'border-emerald-500/60' : 'border-rose-500/60',
      )}
    >
      <header className="flex items-center gap-2">
        <h3
          className={cn(
            'font-mono text-[10px] font-semibold uppercase tracking-[0.14em]',
            tone === 'emerald' ? 'text-emerald-300' : 'text-rose-300',
          )}
        >
          {title}
        </h3>
        <Badge tone={tone}>{articles.length}</Badge>
      </header>

      {articles.length === 0 ? (
        <p className="mt-2 px-0.5 text-[11px] italic text-slate-500">{emptyLabel}</p>
      ) : (
        <ul className="mt-2 max-h-72 space-y-1.5 overflow-y-auto pr-1">
          {articles.map((article, index) => (
            <ArticleRow key={`${article.title}-${index}`} article={article} now={now} />
          ))}
        </ul>
      )}
    </section>
  );
}

/** Aggregate band: label, score, confidence and the +N / -N / ~N split. */
function SentimentSummaryBar({
  summary,
  counts,
}: {
  summary: NewsSentimentSummary;
  counts: { positive: number; negative: number; neutral: number };
}): ReactElement {
  const label = (summary.label || 'NEUTRAL').toUpperCase();
  const tone = label === 'BULLISH' ? 'emerald' : label === 'BEARISH' ? 'rose' : 'slate';
  const score = toNumber(summary.score);

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
      <Badge tone={tone}>{label}</Badge>
      <span
        className={cn(
          'font-mono text-xs font-semibold tabular-nums',
          score === null || score === 0 ? 'text-slate-300' : score > 0 ? 'text-emerald-400' : 'text-rose-400',
        )}
      >
        {formatSignedFixed(summary.score, 3)}
      </span>
      <span className="font-mono text-[10px] tabular-nums text-slate-500">
        confidence {formatQuantity(summary.confidence, 2)}
      </span>

      <span
        className="font-mono text-[11px] tabular-nums text-slate-500 sm:ml-auto"
        title="positive / negative / neutral headline counts"
      >
        <span className="text-emerald-400">+{counts.positive}</span>
        <span className="px-1 text-slate-600">/</span>
        <span className="text-rose-400">-{counts.negative}</span>
        <span className="px-1 text-slate-600">/</span>
        <span className="text-slate-400">~{counts.neutral}</span>
      </span>
    </div>
  );
}

/** Collapsed "Neutral / factual" list, revealed on demand. */
function NeutralSection({ articles, now }: { articles: NewsArticle[]; now: number }): ReactElement | null {
  const [open, setOpen] = useState(false);
  if (articles.length === 0) return null;

  return (
    <div className="border-t border-slate-800/70 pt-2">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="inline-flex items-center gap-1.5 rounded font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500 transition-colors hover:text-slate-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-slate-500"
      >
        <span aria-hidden="true">{open ? '▾' : '▸'}</span>
        Neutral / factual
        <span className="tabular-nums text-slate-600">· {articles.length}</span>
      </button>

      {open ? (
        <ul className="mt-2 grid animate-fade-in grid-cols-1 gap-1.5 lg:grid-cols-2">
          {articles.map((article, index) => (
            <ArticleRow key={`${article.title}-${index}`} article={article} now={now} />
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export interface NewsReportBodyProps {
  report: MarketNewsReport;
  /** Epoch ms used for relative timestamps. */
  now: number;
}

/** Pure rendering of one news report — no fetching, no panel chrome. */
export function NewsReportBody({ report, now }: NewsReportBodyProps): ReactElement {
  const positive = report.positive ?? [];
  const negative = report.negative ?? [];
  const neutral = report.neutral ?? [];
  const hasAny = positive.length + negative.length + neutral.length > 0;

  return (
    <div className="space-y-3">
      <SentimentSummaryBar
        summary={report.sentiment}
        counts={{ positive: positive.length, negative: negative.length, neutral: neutral.length }}
      />

      {report.sources_used.length > 0 ? (
        <p className="font-mono text-[10px] text-slate-600">sources: {report.sources_used.join(', ')}</p>
      ) : null}

      {report.degraded ? (
        <div
          role="status"
          className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2.5"
        >
          <span aria-hidden="true" className="mt-px font-mono text-xs text-amber-300">
            !
          </span>
          <div>
            <p className="font-mono text-[11px] font-semibold uppercase tracking-wider text-amber-200">
              Live news unavailable
            </p>
            <p className="mt-0.5 text-xs text-slate-400">
              The news feeds could not be reached for {report.ticker}. No coverage to split — try
              refreshing in a moment.
            </p>
          </div>
        </div>
      ) : !hasAny ? (
        <EmptyState
          title="No headlines found"
          description={`No recent coverage was returned for ${report.ticker}. Try another ticker.`}
        />
      ) : (
        <>
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            <CoverageColumn
              title="Positive coverage"
              tone="emerald"
              articles={positive}
              now={now}
              emptyLabel="No positive headlines in this batch."
            />
            <CoverageColumn
              title="Negative coverage"
              tone="rose"
              articles={negative}
              now={now}
              emptyLabel="No negative headlines in this batch."
            />
          </div>

          <NeutralSection articles={neutral} now={now} />
        </>
      )}
    </div>
  );
}

/** News coverage split by sentiment — the evidence behind the debate. */
export function NewsSentimentPanel({ className }: NewsSentimentPanelProps): ReactElement {
  const { token } = useAuth();
  const [ticker, setTicker] = useState<string>(DEFAULT_TICKER);
  const [draft, setDraft] = useState<string>('');
  const now = useNow(30_000);

  const news = useMarketNews(token, ticker, NEWS_LIMIT);

  // Ignore a payload fetched for a previously selected ticker (the resource
  // keeps the last value while a new request is in flight).
  const fetched = news.data;
  const report = fetched && fetched.ticker.trim().toUpperCase() === ticker ? fetched : null;

  const selectTicker = useCallback((next: string) => {
    const symbol = next.trim().toUpperCase();
    if (symbol === '') return;
    setTicker(symbol);
  }, []);

  const handleSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      selectTicker(draft);
      setDraft('');
    },
    [draft, selectTicker],
  );

  const subtitle = report
    ? `${report.ticker} · ${formatQuantity(report.headline_count, 0)} headlines · fetched ${formatRelativeTime(
        report.fetched_at,
        now,
      )}`
    : `${ticker} · awaiting headlines`;

  return (
    <Panel
      title="News Sentiment"
      subtitle={subtitle}
      actions={
        <button
          type="button"
          onClick={news.refresh}
          disabled={news.loading}
          title="Refetch headlines"
          aria-label="Refresh news"
          className="inline-flex items-center gap-1.5 rounded-md border border-slate-700 bg-slate-950/60 px-2 py-1 font-mono text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-300 transition-colors hover:bg-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400/60 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {news.loading ? <Spinner className="h-3 w-3" /> : null}
          Refresh
        </button>
      }
      bodyClassName="p-3 sm:p-4"
      className={className}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-slate-500">Ticker</span>
        {NEWS_TICKERS.map((symbol) => (
          <button
            key={symbol}
            type="button"
            onClick={() => selectTicker(symbol)}
            aria-pressed={ticker === symbol}
            className={cn(
              'rounded border px-2 py-0.5 font-mono text-[11px] font-semibold tracking-wide transition-colors duration-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400/60',
              ticker === symbol
                ? 'border-emerald-500/50 bg-emerald-500/10 text-emerald-300'
                : 'border-slate-700 bg-slate-950/60 text-slate-400 hover:border-slate-600 hover:text-slate-200',
            )}
          >
            {symbol}
          </button>
        ))}

        <form onSubmit={handleSubmit} className="flex min-w-0 items-center gap-1.5">
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Other…"
            aria-label="Custom ticker"
            spellCheck={false}
            className="w-24 rounded border border-slate-700 bg-slate-950/60 px-2 py-0.5 font-mono text-[11px] uppercase tracking-wide text-slate-200 transition-colors placeholder:normal-case placeholder:text-slate-600 focus:border-emerald-500/50 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400/60"
          />
          <button
            type="submit"
            disabled={draft.trim() === ''}
            className="rounded border border-slate-700 bg-slate-950/60 px-2 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider text-slate-300 transition-colors hover:bg-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400/60 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Load
          </button>
        </form>
      </div>

      <div className="mt-3">
        {news.error ? (
          report === null ? (
            <ErrorState message={news.error} onRetry={news.refresh} />
          ) : (
            <div className="space-y-3">
              <ErrorState compact message={`Could not refresh — ${news.error}`} onRetry={news.refresh} />
              <NewsReportBody report={report} now={now} />
            </div>
          )
        ) : news.loading && report === null ? (
          <SkeletonRows rows={4} />
        ) : report === null ? (
          <EmptyState title="No headlines loaded" description="Pick a ticker to pull the latest coverage." />
        ) : (
          <NewsReportBody report={report} now={now} />
        )}
      </div>
    </Panel>
  );
}
