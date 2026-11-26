from django.conf import settings

PUBLIC_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
    # MCP clients send MCP-Protocol-Version while discovering the OAuth server.
    "Access-Control-Allow-Headers": "Accept, Content-Type, Content-Length, "
    "Accept-Encoding, Authorization, MCP-Protocol-Version",
    "Access-Control-Max-Age": "3600",
}


def is_public_cors_path(path):
    return path.startswith(tuple(getattr(settings, "PUBLIC_CORS_PATH_PREFIXES", ())))


def add_public_cors_headers(response):
    """Let any origin call the endpoint, without credentials."""
    for name, value in PUBLIC_CORS_HEADERS.items():
        response[name] = value
    return response
