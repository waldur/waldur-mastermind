"""The remote MCP server: Streamable HTTP on /mcp, for PATs and OAuth tokens.

Started by `waldur mcp_server` as its own ASGI process, never inside the API
workers. It is stateless, so replicas need no sticky sessions.
"""

import logging

from mcp import types
from mcp.server.auth.middleware.auth_context import (
    AuthContextMiddleware,
    get_access_token,
)
from mcp.server.auth.provider import AccessToken
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from waldur_core import __version__
from waldur_mastermind.mcp import oauth, tools
from waldur_mastermind.mcp.auth import (
    AuthMiddleware,
    ClientIPMiddleware,
    client_ip_var,
    run_orm_async,
)
from waldur_mastermind.mcp.utils import MCP_PATH, is_mcp_enabled

logger = logging.getLogger(__name__)

INVALID_TOKEN = "This token is no longer valid."


def require_access_token() -> AccessToken:
    # The HTTP middleware refuses these first. Without a token there is no one
    # to act as, whichever way the server was reached.
    token = get_access_token()
    if token is None:
        raise MCPError(code=types.INVALID_REQUEST, message="Authentication required.")
    return token


# The SDK would send the text of an unhandled exception to the client, so both
# handlers catch everything.
async def list_tools(ctx, params) -> types.ListToolsResult:
    token = require_access_token()
    try:
        return types.ListToolsResult(tools=await run_orm_async(tools.list_tools, token))
    except tools.InvalidToken:
        raise MCPError(code=types.INVALID_REQUEST, message=INVALID_TOKEN) from None
    except Exception:
        logger.exception("Listing MCP tools failed.")
        raise MCPError(code=types.INTERNAL_ERROR, message="Internal error.") from None


async def call_tool(ctx, params) -> types.CallToolResult:
    token = require_access_token()
    try:
        return await run_orm_async(
            tools.call_tool,
            token,
            params.name,
            params.arguments or {},
            # Unset only when the server is reached without HTTP.
            client_ip_var.get(None),
        )
    except tools.InvalidToken:
        return tools.error_result(INVALID_TOKEN)
    except Exception:
        logger.exception("MCP tool call failed: %.64s", params.name)
        return tools.error_result("An error occurred while executing the tool.")


def build_mcp() -> Server:
    return Server(
        "Waldur",
        version=__version__,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


# RFC 9728 inserts the well-known part before the resource's path, and a proxy
# can serve /mcp under another path, so every configured path is answered.
RESOURCE_METADATA_ROUTE = oauth.RESOURCE_METADATA_PATH + "{path:path}"
HEALTH_PATH = "/health"


def is_gated(path: str) -> bool:
    return path == MCP_PATH or path.startswith(
        (MCP_PATH + "/", oauth.RESOURCE_METADATA_PATH)
    )


class MCPEnabledGate:
    """Answers 404 on /mcp and its metadata while MCP_ENABLED is off.

    Read on every request, but only for these: other paths don't exist anyway.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and is_gated(scope["path"])
            and not await run_orm_async(is_mcp_enabled)
        ):
            response = JSONResponse({"detail": "Not found."}, status_code=404)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def health(request):
    return JSONResponse({"status": "ok"})


def get_resource_metadata(host: str | None, path: str) -> dict | None:
    resource = oauth.get_resource_url(host, path)
    return oauth.get_resource_metadata(resource) if resource else None


async def resource_metadata(request):
    """RFC 9728 metadata, which tells MCP clients where to get a token."""
    metadata = await run_orm_async(
        get_resource_metadata,
        request.headers.get("host"),
        request.path_params["path"],
    )
    if metadata is None:
        return JSONResponse({"detail": "Not found."}, status_code=404)
    return JSONResponse(metadata)


def build_app():
    mcp_app = build_mcp().streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        # Left to its default, the SDK refuses every Host but localhost, so
        # nothing behind an ingress would get through. Bearer tokens guard /mcp.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
    endpoint = ClientIPMiddleware(AuthMiddleware(AuthContextMiddleware(mcp_app)))
    app = Starlette(
        routes=[
            Route(HEALTH_PATH, health),
            Route(RESOURCE_METADATA_ROUTE, resource_metadata),
            # The SDK would hold a GET open as an event stream, and verify a
            # token for a DELETE that has no session to end. A stateless server
            # needs neither.
            Route(MCP_PATH, endpoint=endpoint, methods=["POST"]),
        ],
        lifespan=mcp_app.router.lifespan_context,
    )
    return MCPEnabledGate(app)
