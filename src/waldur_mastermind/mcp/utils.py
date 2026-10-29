from waldur_core.configuration import config


def is_mcp_enabled() -> bool:
    return config.MCP_ENABLED
