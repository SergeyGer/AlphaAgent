"""Run the AlphaAgent MCP server.

    python -m mcp_server                          # stdio (Claude Code, Cursor)
    python -m mcp_server --transport http         # streamable HTTP microservice
    python -m mcp_server --transport http --port 8100 --host 0.0.0.0

Environment:
    MCP_TRANSPORT   stdio | http   (default: stdio)
    MCP_HOST        bind address for http (default: 0.0.0.0)
    MCP_PORT        bind port for http (default: 8100)
    MCP_LOG_LEVEL   logging level (default: WARNING)
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(prog="mcp_server", description=__doc__)
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "streamable-http", "sse"],
        default=None,
        help="Transport to serve on (default: stdio).",
    )
    parser.add_argument("--host", default=None, help="Bind address for HTTP transports.")
    parser.add_argument("--port", type=int, default=None, help="Bind port for HTTP transports.")
    args = parser.parse_args()

    # Imported here so --help works without booting Django.
    from mcp_server import server

    if args.host:
        server.mcp.settings.host = args.host
    if args.port:
        server.mcp.settings.port = args.port

    transport = args.transport or server.os.environ.get("MCP_TRANSPORT", "stdio")
    server.run(transport)
    return 0


if __name__ == "__main__":
    sys.exit(main())
