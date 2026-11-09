from waldur_core.configuration import config

MCP_PATH = "/mcp"


def is_mcp_enabled() -> bool:
    return config.MCP_ENABLED
