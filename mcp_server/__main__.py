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

    # HTTP transports serve over the network, so they require the shared secret.
    # stdio does not: the caller already has whatever access this process has.
    if transport in ("http", "streamable-http", "sse"):
        from mcp_server.auth import SharedSecretGuard, resolve_secret

        secret = resolve_secret()
        if not secret:
            print(
                "refusing to start: MCP_SHARED_SECRET is not set.\n"
                "The HTTP transports are network-reachable and will not run "
                "unauthenticated. Set MCP_SHARED_SECRET to a random value "
                "(the application sends it as the X-AlphaAgent-MCP-Key header), "
                "or use --transport stdio.",
                file=sys.stderr,
            )
            return 2

        # Built rather than delegated to ``mcp.run()`` so the guard wraps the
        # whole transport, including the initialise handshake and any route the
        # library adds in future.
        import uvicorn

        from mcp_server.server import mcp

        app = SharedSecretGuard(mcp.streamable_http_app(), secret)
        uvicorn.run(
            app,
            host=mcp.settings.host,
            port=mcp.settings.port,
            log_level=mcp.settings.log_level.lower(),
        )
        return 0

    server.run(transport)
    return 0


if __name__ == "__main__":
    sys.exit(main())
