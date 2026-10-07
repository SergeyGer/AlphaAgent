# 0005. Expose market and news tools as an MCP server

- **Status:** Accepted
- **Date:** 2026-09-30
- **Deciders:** Project maintainer
- **Related:** [ADR-0002](0002-adversarial-three-agent-debate.md)

## Context

The market and news tools were Python objects passed directly into the CrewAI
agents. They worked, but they were useful only inside this process: any other
client — an editor assistant, a local model, another agent framework — would have
had to import this codebase to reuse them.

Rejected alternatives, recorded in the wiki:

- *A REST wrapper.* Doesn't compose with AI tooling, which is the entire point.
- *Publish the tools as a library.* Requires consumers to run Python and import
  this codebase.

*Date note: 2026-09-30 is the initial-release commit (`f6f3397`) that shipped this
design; the decision itself predates it and no earlier dated record exists.*

## Decision

We serve the read-only market and news tools over the Model Context Protocol
(`mcp_server/`), supporting both stdio and streamable HTTP, and let the crew
consume them over MCP when `AI_TOOLS_VIA_MCP=true`. The in-process tools remain
the default. The MCP surface is market data only: it has no database access and
cannot place a trade.

## Consequences

### Positive

- Any MCP client — Claude Code, Cursor, a local Ollama agent, another CrewAI crew —
  can use the same tools with no changes to this codebase.
- One implementation, two consumers: when MCP routing is enabled, the agent path
  and the external path are literally the same code.
- Exposing the server is not a privilege escalation, because execution stays
  behind the REST API and the execution guard ([ADR-0001](0001-deterministic-execution-guard.md)).
- Tool inputs are clamped rather than trusted (for example the news `limit`), so an
  external client cannot ask for unbounded data.

### Negative / trade-offs

- One more service to operate, health-check and monitor, with its own container
  and port.
- A network hop is added to the tool path when MCP mode is enabled, and a failure
  there degrades agent runs.
- Two ways to wire the same tools means two configurations to test; the in-process
  default means the MCP path is the less-exercised one in production.
- The tool contract is now a public interface. Changing a tool's return shape can
  break clients outside this repository.

### Neutral

- Transport is configuration, not code: stdio for a local client, streamable HTTP
  for the containerised deployment.
