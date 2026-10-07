# MCP Tool Server

`mcp_server/` exposes AlphaAgent's market and news data over the **Model Context Protocol**
(MCP), an open JSON-RPC protocol in which a server advertises typed *tools* and any MCP-capable
client discovers and calls them. The server is a thin, strictly read-only adapter over the same
service layer the CrewAI crew uses.

Why it exists, in the words of `mcp_server/server.py`:

> The news scraper and the market-data client used to be private Python functions reachable only
> from inside the CrewAI crew. Wrapping them in an MCP server turns them into a
> **protocol-level capability**: any MCP-capable client (Claude Code, Cursor, a local Ollama
> agent, another CrewAI crew, a LangGraph workflow) can call them without a line of backend code
> changing.

Two consequences matter for the architecture:

1. **One implementation, two consumers.** The crew can source its tools from this server
   instead of calling the service layer in-process (`AI_TOOLS_VIA_MCP=true`), which means the
   agent path and the external path are literally the same code. See [[Agent-Debate]].
2. **A hard capability boundary.** The MCP surface is market data only. Execution stays behind
   the REST API and the guard — see [[Execution-Guard]] and [[API-Reference]].

| Property | Value | Source |
| --- | --- | --- |
| Implementation | `mcp_server/server.py` (FastMCP), entrypoint `mcp_server/__main__.py` | — |
| SDK | `mcp` 1.28.1 in this venv (transitive dependency of `crewai==1.15.23`) | `pip show crewai` → `mcp` |
| Server name / identity | `alphaagent-market-data` | `FastMCP(name=...)`, asserted in `test_mcp` |
| Tools | 6, all read-only | `@mcp.tool()` declarations |
| Default transport | `stdio` | `run(transport="stdio")` |
| HTTP bind | `0.0.0.0:8100`, path `/mcp` | `MCP_HOST`, `MCP_PORT`, `mcp.settings.streamable_http_path` |
| Compose service | `mcp` (container `alpha_mcp`), port `8100:8100` | `docker-compose.yml` |

## Tools

Every tool returns a **JSON string**. `_json()` serialises `Decimal` as a string (never a float)
and dataclasses via their `as_dict()`. Tool names are declared with `@mcp.tool()` and the
docstrings are the tool descriptions an LLM client sees — `test_mcp` asserts every tool has a
description longer than 30 characters, because "an undocumented tool is unusable by an LLM
client".

| Tool | Arguments | Returns |
| --- | --- | --- |
| `get_market_price` | `ticker: str` | Latest quote: `price`, `previous_close`, `change_pct`, `currency`, `source`, `as_of`. `source` is `yfinance`, `cache` or `fallback`. |
| `get_news_sentiment` | `ticker: str`, `limit: int = 10` | Aggregated `sentiment` (label/score/confidence), `articles[]` each with `polarity` and `sentiment`, plus `positive_count`, `negative_count`, `neutral_count`, `headline_count`, `sources_used`, `degraded`. |
| `get_price_history` | `ticker: str`, `period: str = "1y"` | `latest`, `period_high`, `period_low`, `change_pct`, `sma_50`, `sma_200`, `drawdown_from_high_pct`, `below_sma_50`, `below_sma_200`, `degradation_flags`, and `points` — a `[date, close]` array for charting. |
| `get_financial_health` | `ticker: str` | `total_debt`, `total_cash`, `debt_to_equity`, `current_ratio`, `profit_margin`, `free_cash_flow`, `trailing_pe`, `analyst_target`, `recommendation_key`, `sector`, `red_flags[]`, `degraded`. |
| `get_market_snapshot` | `ticker: str` | `price`, `history`, `fundamentals`, `news` (sentiment, counts and `top_headlines`) in one call, with a top-level `degraded` flag. |
| `list_watchlist` | none | `tickers[]` and `count`, read from the `ALPHA_WATCHLIST` setting. |

Notes that are verifiable in the source:

- **`limit` is clamped, not trusted.** `get_news_sentiment` computes
  `max(1, min(int(limit or 10), 30))` before calling `build_news_report`, so a client cannot ask
  for 10 000 headlines. (A `limit` of `0` or `None` becomes the default 10.)
- **`period` falls back to `1y`** via `period or "1y"`; the default window is what makes the
  200-day average computable.
- **`get_market_snapshot` is a convenience fan-in**, not a new data source: it calls
  `get_latest_quote`, `_price_history`, `_financial_health` and `build_news_report`, and reuses
  the quote price for the fundamentals call. Its `degraded` flag is true only when the quote,
  the history *and* the fundamentals are all missing.
- **The internal imports are aliased** (`_financial_health`, `_price_history`) because the
  `@mcp.tool()` functions intentionally share those names — an unaliased import would make each
  tool call itself recursively.
- **Degradation is part of the contract.** Every tool returns an explicit
  `{"ticker": ..., "error": ..., "degraded": true}` payload instead of raising when an upstream
  provider is unavailable. `get_financial_health` never returns `None`: an unavailable ticker —
  including any crypto pair, which has no balance sheet — yields a degraded object, so a
  consumer can state that it found no evidence rather than crashing.

## Transports

`run()` normalises the requested transport (`http` → `streamable-http`; `sse` is also accepted;
anything unrecognised falls back to `stdio`) and calls `mcp.run(transport=...)`.

| Transport | How to start | Use when | Behaviour |
| --- | --- | --- | --- |
| `stdio` (default) | `python -m mcp_server` | A local MCP client that spawns the process: Claude Code, Cursor, a local agent. | The client owns the process; JSON-RPC frames travel over stdin/stdout. |
| `streamable-http` | `python -m mcp_server --transport http` → serves `http://0.0.0.0:8100/mcp` | The containerised microservice that other services (and the worker) call over the network. | Long-lived HTTP endpoint, one service for many clients. |

Under `stdio`, **stdout belongs to the protocol**. `_force_logging_to_stderr()` removes any
root `StreamHandler` writing to stdout and installs a stderr handler — and it is called twice,
because Django's `LOGGING` config installs its own stdout handler during `django.setup()`. The
module docstring is explicit that this is "a correctness requirement, not a preference", and
`test_mcp.McpLoggingTests` asserts no root stream handler targets `sys.stdout`. Default level is
`WARNING`, overridable with `MCP_LOG_LEVEL` (the compose service sets `INFO`).

Host and port come from `MCP_HOST` / `MCP_PORT` (defaults `0.0.0.0` / `8100`) and can be
overridden per-invocation with `--host` / `--port`, which mutate `mcp.settings` before `run()`.

In `docker-compose.yml` the service is:

```yaml
mcp:
  <<: *app
  command: [python, -m, mcp_server, --transport, http, --host, 0.0.0.0, --port, "8100"]
  environment:
    <<: *app_env
    MCP_LOG_LEVEL: INFO
  ports: ["8100:8100"]
  healthcheck:
    test: [CMD-SHELL, "python -c \"import socket; socket.create_connection(('127.0.0.1', 8100), 3)\""]
```

The health check is a **TCP connect**, not an HTTP request, because the MCP endpoint expects a
JSON-RPC handshake. Binding `0.0.0.0` and publishing the port is deliberate — the tools serve
public market data and are read-only; the compose comment notes the mapping can be dropped if
only the worker needs them. `mcp_server/*` is exempted from ruff's `S104`
(hardcoded-bind-all-interfaces) for this reason.

## The read-only safety boundary

> No tool reads or writes portfolios, positions, recommendations or the ledger. Nothing here can
> place a trade. Execution stays behind the REST API and its guard, so a compromised or
> over-eager agent cannot move money through MCP.

This is enforced by construction and then *pinned by tests*
(`test_mcp.McpIsolationTests`), because a claim about a negative is only as good as its check:

| Test | Assertion |
| --- | --- |
| `test_tools_do_not_query_the_database` | Calls all five data tools inside `CaptureQueriesContext` with a real portfolio in the database and asserts **zero ORM queries**. |
| `test_no_tool_name_suggests_account_access` | No tool name may contain `portfolio`, `trade`, `buy`, `sell`, `execute`, `approve`, `balance`, `position`, `transaction`, `recommendation`, `credential` or `token`. |
| `test_tool_descriptions_do_not_advertise_mutations` | No description may say "place a trade", and "execute" may not appear outside a "cannot" clause. |

The only Django coupling in the tool surface is `django.conf.settings` in `list_watchlist`,
which reads the `ALPHA_WATCHLIST` list (default `AAPL,TSLA,BTC`) — no model imports. Django is
still booted at import time (`django.setup()`) because the tools delegate to the shared service
layer (`services.market_data`, `services.news`, `services.fundamentals`) and because settings
must resolve consistently; but no tool opens a database connection. The cache reads inside those
services go to the Django cache backend, not the ORM.

The two-call pattern is the boundary in practice:

```
MCP (read-only evidence)  →  LLM proposal  →  evaluate_proposal guard  →  ledger
                                              ^ the only DB-mutating path
```

An MCP client can therefore gather everything needed to form an opinion and still be unable to
act on it. See [[Execution-Guard]] for the guardrail rules themselves.

## How the CrewAI agents consume the same tools

Routing is opt-in:

| Setting | Env var | Default | Effect |
| --- | --- | --- | --- |
| `AI_CONFIG["TOOLS_VIA_MCP"]` | `AI_TOOLS_VIA_MCP` | `false` | Route the crew's tools through MCP instead of calling the service layer in-process. |
| `AI_CONFIG["MCP_SERVER_URL"]` | `AI_MCP_SERVER_URL` | `""` (compose: `http://mcp:8100/mcp`) | The streamable-HTTP endpoint. |

`AlphaAgentOrchestrator.mcp_servers(allowed)` returns `[]` — so the caller falls back to
in-process tools — when routing is disabled, when the URL is empty (logging
`TOOLS_VIA_MCP is on but AI_MCP_SERVER_URL is empty`), or when `crewai.mcp.config.MCPServerHTTP`
cannot be imported (older CrewAI). Otherwise it returns exactly one reference:

```python
def predicate(tool: dict) -> bool:
    # CrewAI namespaces MCP tool names, so match on the suffix.
    name = str(tool.get("name", ""))
    return any(name.endswith(suffix) for suffix in allowed)


return [MCPServerHTTP(url=url, streamable=True, tool_filter=predicate)]
```

The `tool_filter` is the mechanism that gives each agent only the tools its role needs. CrewAI
prefixes MCP tool names (e.g. `mcp_get_market_price`), so the predicate matches on the
**suffix** rather than equality — `test_enabled_with_a_url_returns_one_filtered_reference`
asserts `predicate({"name": "mcp_get_market_price"})` is true while the news tool is false.

| Agent | Tools allowed over MCP | In-process fallback when MCP is off |
| --- | --- | --- |
| Bullish Research Analyst | `get_news_sentiment`, `get_price_history` | `tools["news"]`, `tools["history"]` (2) |
| Risk Assessor (Short Seller) | `get_financial_health`, `get_price_history`, `get_news_sentiment` | `tools["financial"]`, `tools["history"]`, `tools["news"]` (3) |
| Chief Investment Officer (CIO) | `get_market_price` | `tools["market"]` (1) |

When MCP routing is live the agents are constructed with `tools=[]` and `mcps=<refs>`, so there
is exactly one tool surface per agent — no duplicated or shadowed capabilities.
`test_crew_uses_mcp_refs_and_no_local_tools_when_enabled` asserts `len(agent.tools) == 0` and
`len(agent.mcps or []) == 1` for all three; the disabled-path test asserts tool counts
`[2, 3, 1]` and empty `mcps`. Default is off because in-process calls are one less network hop
(`.env.example`). See [[Agent-Debate]] for the bull/bear/CIO design.

## Connecting from Claude Code or Cursor

Only the server side is verified here (tool list, transports, bind address, path). The client
snippets below use each client's documented MCP configuration format; they were not executed
against a running client in this repository.

Start the server the way the client will, and confirm it boots:

```bash
cd /root/projects/AlphaAgent
venv/bin/python -m mcp_server          # stdio: prints nothing, waits on stdin. Ctrl-C to exit.
venv/bin/python -m mcp_server --help   # --help works without booting Django
```

Two environment facts make the stdio launch work from a client:

- `server.py` calls `os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")`, and
  `config/settings.py` loads `.env` from an **absolute** `BASE_DIR`, so secrets do not need to be
  duplicated into the client config — a local `.env` is enough.
- `python -m mcp_server` resolves the package from `sys.path`, so the client must either launch
  with the repository as the working directory or set `PYTHONPATH`.

**stdio — Claude Code** (registered once, from the repository root):

```bash
claude mcp add alphaagent -- /root/projects/AlphaAgent/venv/bin/python -m mcp_server
```

**stdio — Cursor** (`.cursor/mcp.json`, absolute paths, no secrets):

```json
{
  "mcpServers": {
    "alphaagent": {
      "command": "/root/projects/AlphaAgent/venv/bin/python",
      "args": ["-m", "mcp_server"],
      "env": { "PYTHONPATH": "/root/projects/AlphaAgent" }
    }
  }
}
```

**Streamable HTTP** — start the service, then point clients at the URL instead of a command:

```bash
docker compose up -d mcp          # or: venv/bin/python -m mcp_server --transport http
claude mcp add --transport http alphaagent http://127.0.0.1:8100/mcp
```

```json
{ "mcpServers": { "alphaagent": { "url": "http://127.0.0.1:8100/mcp" } } }
```

Reachability check that does not require an MCP client (the same probe the compose health check
uses — a JSON-RPC handshake would be needed for anything deeper):

```bash
python -c "import socket; socket.create_connection(('127.0.0.1', 8100), 3); print('reachable')"
```

For a protocol-level verification without a client, `core/tests/test_mcp.py` drives the real
server through the official SDK over an in-memory transport — including the handshake, the
advertised tool list and every payload shape. See [[Quality-and-Testing]].

Related pages: [[Architecture]], [[Agent-Debate]], [[Execution-Guard]], [[API-Reference]],
[[Configuration]], [[Operations]], [[Quality-and-Testing]], [[Engineering-Decisions]].
