"""Semantic routing for AI Assistant tool selection.

Uses embedding-based similarity to pre-filter which tools are sent to the LLM,
reducing token usage and improving accuracy.
"""

import logging

from semantic_router import Route
from semantic_router.encoders import HuggingFaceEncoder
from semantic_router.routers import SemanticRouter

from waldur_mastermind.chat.tools.enums import ToolName

logger = logging.getLogger(__name__)

# Singleton — router is initialized lazily on first use.
_router: SemanticRouter | None = None
_route_to_tool: dict[str, ToolName] = {}


def _build_router() -> SemanticRouter:
    """Build a SemanticRouter from all registered tool definitions."""
    from waldur_mastermind.chat.tools.registry import tool_registry

    global _route_to_tool

    routes = []
    _route_to_tool = {}

    for name, tool in tool_registry._tools.items():
        utterances = tool.definition.route_utterances
        if not utterances:
            continue
        route = Route(name=name.value, utterances=utterances)
        routes.append(route)
        _route_to_tool[name.value] = name

    if not routes:
        logger.warning("No tools have route_utterances; semantic routing disabled.")
        return None

    encoder = HuggingFaceEncoder()
    router = SemanticRouter(
        encoder=encoder,
        routes=routes,
        auto_sync="local",
    )
    logger.info(
        "Semantic router initialized with %d routes (%d tools have utterances).",
        len(routes),
        len(routes),
    )
    return router


def get_router() -> SemanticRouter | None:
    """Return the singleton SemanticRouter, building it on first call."""
    global _router
    if _router is None:
        try:
            _router = _build_router()
        except Exception:
            logger.exception("Failed to initialize semantic router; falling back.")
            _router = None
    return _router


def get_relevant_tools(
    query: str,
    user_tools: list[ToolName],
    max_tools: int = 6,
) -> list[ToolName]:
    """Return the most relevant tools for a query using semantic similarity.

    Args:
        query: The user's chat input.
        user_tools: Tools the user has access to (from tool_sets).
        max_tools: Maximum number of tools to return.

    Returns:
        Filtered list of ToolName. Falls back to *user_tools* when semantic
        routing is unavailable or no route matches.
    """
    router = get_router()
    if router is None:
        return user_tools

    try:
        result = router(query)
    except Exception:
        logger.exception("Semantic routing failed for query; falling back.")
        return user_tools

    if result.name is None:
        # No confident match — send all tools, let LLM decide.
        return user_tools

    # Collect the matched tool plus related tools in the same domain.
    matched_name = result.name
    matched_tool = _route_to_tool.get(matched_name)

    if matched_tool is None or matched_tool not in user_tools:
        return user_tools

    # Build relevant set: the matched tool + semantically nearby tools.
    # We use route_many if available, otherwise just the single best match
    # plus tools that share a domain prefix (e.g., all proposal tools together).
    relevant = {matched_tool}

    # Group related tools: if matched tool is proposal-related, include all proposal tools
    _PROPOSAL_TOOLS = {
        ToolName.FIND_MATCHING_CALLS,
        ToolName.GUIDE_PROPOSAL,
        ToolName.REVIEW_WORKLOAD,
        ToolName.CALL_INSIGHTS,
        ToolName.PROPOSAL_OVERVIEW,
        ToolName.REVIEW_ASSISTANT,
    }
    _VM_TOOLS = {
        ToolName.CREATE_VM,
        ToolName.PREVIEW_VM,
        ToolName.LIST_PROJECTS,
        ToolName.SHOW_USER_RESOURCES,
    }

    if matched_tool in _PROPOSAL_TOOLS:
        relevant.update(_PROPOSAL_TOOLS)
    elif matched_tool in _VM_TOOLS:
        relevant.update(_VM_TOOLS)

    # Filter to tools the user actually has access to
    filtered = [t for t in user_tools if t in relevant]

    # If filtering is too aggressive (< 2 tools), fall back to all
    if len(filtered) < 2:
        return user_tools

    # Cap at max_tools
    return filtered[:max_tools]
