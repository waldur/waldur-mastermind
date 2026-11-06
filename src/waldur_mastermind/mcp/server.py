"""The remote MCP server: Streamable HTTP on /mcp, authenticated by PATs.

Started by `waldur mcp_server` as its own ASGI process, never inside the API
workers. It is stateless, so replicas need no sticky sessions.
"""

import json

from mcp import types
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from waldur_core import __version__
from waldur_mastermind.mcp.auth import (
    ClientIPMiddleware,
    WaldurTokenVerifier,
    run_orm_async,
)
from waldur_mastermind.mcp.utils import is_mcp_enabled

MCP_PATH = "/mcp"

# Placeholder until the chat tool registry is adapted.
WHOAMI = types.Tool(
    name="whoami",
    description="Return the Waldur user this connection acts as.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    output_schema={
        "type": "object",
        "properties": {"uuid": {"type": "string"}, "username": {"type": "string"}},
        "required": ["uuid", "username"],
        "additionalProperties": False,
    },
    annotations=types.ToolAnnotations(read_only_hint=True),
)


async def list_tools(ctx, params) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[WHOAMI])


async def call_tool(ctx, params) -> types.CallToolResult:
    if params.name != WHOAMI.name:
        message = f"Unknown tool: {params.name!r}"
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=message)], is_error=True
        )
    claims = get_access_token().claims
    data = {"uuid": claims["sub"], "username": claims["username"]}
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(data))],
        structured_content=data,
    )


def build_mcp() -> Server:
    return Server(
        "Waldur",
        version=__version__,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


class MCPEnabledGate:
    """Answers 404 on /mcp while MCP_ENABLED is off, read on every request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and (scope["path"] == MCP_PATH or scope["path"].startswith(MCP_PATH + "/"))
            and not await run_orm_async(is_mcp_enabled)
        ):
            response = JSONResponse({"detail": "Not found."}, status_code=404)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def health(request):
    return JSONResponse({"status": "ok"})


def build_app():
    mcp_app = build_mcp().streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        # Left to its default, the SDK refuses every Host but localhost, so
        # nothing behind an ingress would get through. Bearer tokens guard /mcp.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
        # Without auth settings the token middleware isn't installed. The issuer
        # is required but unused until #808 publishes resource metadata.
        auth=AuthSettings(issuer_url="http://localhost", resource_server_url=None),
        token_verifier=WaldurTokenVerifier(),
    )
    app = Starlette(
        routes=[
            Route("/health", health),
            # The SDK would hold a GET open as an event stream, and verify a
            # token for a DELETE that has no session to end. A stateless server
            # needs neither.
            Route(MCP_PATH, endpoint=ClientIPMiddleware(mcp_app), methods=["POST"]),
        ],
        lifespan=mcp_app.router.lifespan_context,
    )
    return MCPEnabledGate(app)
