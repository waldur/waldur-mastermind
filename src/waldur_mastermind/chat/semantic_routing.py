"""Semantic routing for AI Assistant tool selection.

Uses embedding-based similarity to pre-filter which tools are sent to the LLM,
reducing token usage and improving accuracy.
"""

import logging

import numpy as np
from fastembed import TextEmbedding

from waldur_mastermind.chat.tools.enums import ToolName

logger = logging.getLogger(__name__)

# Singleton — encoder and route index are initialized lazily on first use.
_encoder: TextEmbedding | None = None
_route_embeddings: np.ndarray | None = None
_route_names: list[ToolName] = []


def _get_encoder() -> TextEmbedding:
    global _encoder
    if _encoder is None:
        _encoder = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")
    return _encoder


def _build_route_index() -> None:
    """Build route embeddings from all registered tool definitions."""
    from waldur_mastermind.chat.tools.registry import tool_registry

    global _route_embeddings, _route_names

    utterances_per_route: list[tuple[ToolName, list[str]]] = []
    for name, tool in tool_registry._tools.items():
        utts = tool.definition.route_utterances
        if utts:
            utterances_per_route.append((name, utts))

    if not utterances_per_route:
        logger.warning("No tools have route_utterances; semantic routing disabled.")
        return

    encoder = _get_encoder()

    # Compute a single embedding per route by averaging its utterance embeddings.
    route_vecs = []
    names = []
    for tool_name, utts in utterances_per_route:
        embeddings = np.array(list(encoder.embed(utts)))  # (n_utterances, dim)
        centroid = embeddings.mean(axis=0)
        centroid /= np.linalg.norm(centroid)
        route_vecs.append(centroid)
        names.append(tool_name)

    _route_embeddings = np.stack(route_vecs)  # (n_routes, dim)
    _route_names = names

    logger.info(
        "Semantic router initialized with %d routes.",
        len(names),
    )


def _ensure_index() -> bool:
    """Ensure the route index is built. Returns True if ready."""
    if _route_embeddings is None:
        try:
            _build_route_index()
        except Exception:
            logger.exception("Failed to initialize semantic router; falling back.")
    return _route_embeddings is not None


def get_relevant_tools(
    query: str,
    user_tools: list[ToolName] | None,
    max_tools: int = 6,
) -> list[ToolName] | None:
    """Return the most relevant tools for a query using semantic similarity.

    Args:
        query: The user's chat input.
        user_tools: Tools the user has access to (from tool_sets). ``None``
            means unrestricted access to all registered tools.
        max_tools: Maximum number of tools to return.

    Returns:
        Filtered list of ToolName, or ``None`` to preserve the "all tools"
        sentinel. Falls back to *user_tools* when semantic routing is
        unavailable or no route matches.
    """
    if not _ensure_index():
        return user_tools

    try:
        encoder = _get_encoder()
        query_vec = np.array(list(encoder.embed([query])))[0]
        query_vec /= np.linalg.norm(query_vec)

        # Cosine similarities (embeddings are already normalized).
        similarities = _route_embeddings @ query_vec  # (n_routes,)
        best_idx = int(np.argmax(similarities))
        best_score = similarities[best_idx]
    except Exception:
        logger.exception("Semantic routing failed for query; falling back.")
        return user_tools

    # Require a minimum confidence to filter.
    if best_score < 0.4:
        return user_tools

    matched_tool = _route_names[best_idx]

    if matched_tool is None:
        return user_tools
    # When the user is restricted, the matched tool must be in their set.
    if user_tools is not None and matched_tool not in user_tools:
        return user_tools

    # Build relevant set: the matched tool + semantically nearby tools.
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

    # Filter to tools the user actually has access to. None means unrestricted.
    if user_tools is None:
        filtered = list(relevant)
    else:
        filtered = [t for t in user_tools if t in relevant]

    # If filtering is too aggressive (< 2 tools), fall back to all
    if len(filtered) < 2:
        return user_tools

    # Cap at max_tools
    return filtered[:max_tools]
