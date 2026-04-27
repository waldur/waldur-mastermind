"""Rejection and utility prompts for the AI Assistant (templates with {organization} placeholder).

The canned rejection avoids "I'm sorry" phrasing to stay consistent with the persona's
apology rule (apologize only for factual errors). A refusal is a boundary, not an apology.
"""

from waldur_mastermind.chat.tools.enums import ToolCategory
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.tool_sets import get_tool_set_for_user

REJECTION_SYSTEM_PROMPT_TEMPLATE = (
    "The user's latest message could not be processed. Generate a brief response "
    "declining that specific request and suggesting {organization} cloud management "
    "tasks you can assist with instead. Do not apologize for the situation, and do "
    "not mention content filters, safety systems, or detection mechanisms."
)

# Human-readable phrase for each tool category. Iterated in ToolCategory
# declaration order so the rendered list is deterministic. Adding a new
# ToolCategory without a phrase here will fail the test suite — see
# ``test_canned_rejection.test_every_category_has_a_phrase``.
CATEGORY_PHRASES: dict[ToolCategory, str] = {
    ToolCategory.MARKETPLACE: "exploring marketplace offerings",
    ToolCategory.VM: "creating VMs",
    ToolCategory.ACCOUNT: "viewing your resources or projects",
    ToolCategory.PROPOSALS_RESEARCHER: "proposal-call workflows",
    ToolCategory.PROPOSALS_REVIEWER: "reviewing proposals",
}

_CANNED_REJECTION_BASE = "I can't help with that request."
_CANNED_REJECTION_TRY_AGAIN = "Try again with a different question."


def _join_phrases(phrases: list[str]) -> str:
    if len(phrases) == 1:
        return phrases[0]
    return f"{', '.join(phrases[:-1])}, and {phrases[-1]}"


def _enabled_categories(user) -> set[ToolCategory]:
    """Collect tool categories from the user's actually-permitted tool set.

    ``get_tool_set_for_user`` returns ``None`` for internal callers without
    user context — treat that as "all categories" so the rejection isn't
    silently emptied for that path.
    """
    tool_set = get_tool_set_for_user(user)
    if tool_set is None:
        return set(ToolCategory)
    categories: set[ToolCategory] = set()
    for tool_name in tool_set:
        tool = tool_registry.get(tool_name)
        if tool is not None and tool.definition.category is not None:
            categories.add(tool.definition.category)
    return categories


def build_canned_rejection(user, organization: str) -> str:
    """Compose the static rejection sent when no thread history exists.

    Capability examples are derived from the user's actually-enabled tools
    so the message cannot drift from ``tool_sets.py``.
    """
    enabled = _enabled_categories(user)
    phrases = [CATEGORY_PHRASES[c] for c in CATEGORY_PHRASES if c in enabled]
    if not phrases:
        return f"{_CANNED_REJECTION_BASE} {_CANNED_REJECTION_TRY_AGAIN}"
    return (
        f"{_CANNED_REJECTION_BASE} I can assist with {organization} tasks like "
        f"{_join_phrases(phrases)}. {_CANNED_REJECTION_TRY_AGAIN}"
    )


TITLE_GENERATION_PROMPT = (
    "Generate a very short title (3-6 words) for this conversation. "
    "Reply with only the title text, nothing else. "
    "Do not wrap the title in quotation marks.\n\n"
    "User message: "
)
