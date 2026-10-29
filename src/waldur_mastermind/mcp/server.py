"""The remote MCP server: Streamable HTTP on /mcp, authenticated by PATs.

Started by `waldur mcp_server` as its own ASGI process, never inside the API
workers. It is stateless, so replicas need no sticky sessions.
"""

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from waldur_mastermind.mcp.auth import WaldurTokenVerifier, run_orm_async
from waldur_mastermind.mcp.utils import is_mcp_enabled

MCP_PATH = "/mcp"


def build_mcp() -> FastMCP:
    mcp = FastMCP("Waldur", auth=WaldurTokenVerifier())

    # Placeholder until the chat tool registry is adapted.
    @mcp.tool(annotations={"readOnlyHint": True})
    def whoami() -> dict:
        """Return the Waldur user this connection acts as."""
        claims = get_access_token().claims
        return {"uuid": claims["sub"], "username": claims["username"]}

    return mcp


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
    mcp_app = build_mcp().http_app(path=MCP_PATH, stateless_http=True)
    app = Starlette(
        routes=[Route("/health", health), Mount("/", app=mcp_app)],
        lifespan=mcp_app.lifespan,
    )
    return MCPEnabledGate(app)
