# AlphaAgent — Frontend

Dark, terminal-style React SPA for the AlphaAgent autonomous investment platform.
Talks to the existing Django REST + WebSocket backend; **no backend code lives here**.

## Stack

| Concern    | Choice                                    |
| ---------- | ----------------------------------------- |
| Build      | Vite 5                                    |
| UI         | React 18 + TypeScript (strict)            |
| Styling    | Tailwind CSS 3 (dark slate terminal theme)|
| Charts     | Recharts 2 (`AreaChart` + donut `PieChart`)|
| Data       | `fetch` + native `WebSocket` (no extra deps)|

## Getting started

```bash
npm install
npm run dev        # http://localhost:5173 (proxies /api and /ws to 127.0.0.1:8000)
npm run build      # tsc --noEmit && vite build  → dist/
npm run typecheck  # tsc --noEmit
npm run preview    # serve the production build
```

### Environment

| Variable       | Default | Purpose                                                                 |
| -------------- | ------- | ----------------------------------------------------------------------- |
| `VITE_API_BASE`| `""`    | REST base URL. Empty ⇒ same-origin relative requests (recommended).      |
| `VITE_WS_BASE` | `""`    | Optional explicit WebSocket origin; otherwise derived from the page (or from `VITE_API_BASE` when absolute). |

Copy `.env.example` to `.env` to override. In development `vite.config.ts`
proxies `/api` and `/ws` (with `ws: true`) to `http://127.0.0.1:8000`.

## Architecture

```
src/
├── api/client.ts          fetch wrapper, token storage, 401 broadcast,
│                          endpoint map, WebSocket URL builder
├── hooks/
│   ├── useAuth.tsx        AuthProvider: login / logout / token persistence
│   ├── usePortfolioStream.ts  the only WebSocket owner (backoff 1s→30s)
│   ├── useAsyncResource.ts    generic abortable fetch+loading+error primitive
│   ├── usePortfolio.ts · useSnapshots.ts · useTransactions.ts
│   ├── useDecisionLogs.ts · useRecommendations.ts (approve/reject, optimistic)
│   ├── useMarketNews.ts   news coverage split by sentiment
│   ├── useToasts.ts · useNow.ts · useValueFlash.ts
├── lib/
│   ├── format.ts          formatCurrency / formatQuantity / formatPercent /
│   │                      formatRelativeTime (+ never renders NaN)
│   ├── feed.ts            decision-log & agent.thinking → unified feed rows
│   ├── stream.ts          WebSocket frame validation
│   ├── colors.ts          chart palette
│   └── cn.ts              class-name joiner
├── components/            Header, MetricsGrid, EquityChart, AllocationChart,
│                          ThoughtsFeed, NewsSentimentPanel, RecommendationsPanel,
│                          TradesTable, …
├── types.ts               every REST + WebSocket payload interface
├── App.tsx                auth gate (login screen ⇄ dashboard)
└── main.tsx
```

### Auth

`POST /api/auth/token/` with `{username, password}` returns `{token}`. The token
is stored in `localStorage` and sent as `Authorization: Token <token>` on every
REST call. **Any 401** clears the token and returns to the login screen — the
`onUnauthorized` broadcast in `api/client.ts` is what drives that.

### Realtime

`usePortfolioStream` owns `ws(s)://<host>/ws/portfolio/?token=<token>`:

* exponential backoff reconnection, 1s → 30s (with ±20 % jitter),
* every frame validated against the frozen contract before use,
* on **reconnect** the whole REST state is refetched (portfolio, snapshots,
  transactions, logs, recommendations),
* the header pill shows `LIVE` (pulsing) or `RECONNECTING / DISCONNECTED` with a
  live countdown and a manual *Retry*.

Handled frames: `portfolio.snapshot`, `trade.executed`, `decision.created`,
`agent.thinking`, `recommendation.created`, `recommendation.updated`,
`autonomy.changed`.

### Debate + news sentiment

* `decision.created` frames and `GET /api/portfolio/logs/` rows now carry
  `bull_case` (research analyst) and `bear_case` (risk assessor / short seller)
  alongside the CIO's `reasoning`. Each AI-thought row expands into **🐂 Bull
  Case** (emerald), **🐻 Bear Case** (rose) and a neutral **⚖️ CIO Verdict**;
  blank sections render "Not available for this decision." and long arguments
  scroll inside their card. Collapsed rows keep the 3-line reasoning clamp.
* `NewsSentimentPanel` reads `GET /api/market/news/?ticker=&limit=` (auth
  required) and splits coverage into positive / negative columns plus a
  collapsed neutral list. The ticker selector (AAPL / TSLA / BTC presets + free
  text) is local panel state, so switching tickers never refetches the
  dashboard. `degraded: true` renders a *Live news unavailable* notice instead
  of empty columns.

### Data handling rules

* All decimals arrive as **strings** — they are parsed with `Number()` inside
  the shared formatters, never inline. Unparseable values render `—`, so the UI
  cannot display `NaN`/`undefined`.
* Numbers everywhere use `font-mono` with tabular figures.
* Metric values flash emerald/rose for ~900 ms when a WebSocket push changes them.
* The AI thoughts feed is capped at 100 in-memory rows, newest first, de-duplicated
  against the persisted REST log.
* Every panel implements loading (skeleton), error (retry) and empty states.
