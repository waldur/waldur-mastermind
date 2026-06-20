from waldur_mastermind.chat.tools.enums import ToolName

# Tools grouped by category — mirrors the subfolder layout under tools/.
# Per-role sets below compose these lists instead of repeating tool names.

_VM_TOOLS: list[ToolName] = [
    ToolName.CREATE_VM,
    ToolName.PLAN_VM,
    ToolName.DISPLAY_USER_RESOURCES,
]

_ACCOUNT_TOOLS: list[ToolName] = [
    ToolName.LIST_ORGANIZATIONS,
    ToolName.LIST_PROJECTS,
    ToolName.GET_PROJECT_RESOURCES,
    ToolName.GET_PROJECT_QUOTA,
    ToolName.GET_RESOURCE_USAGE,
    ToolName.EXPLAIN_PROJECT_CREDIT_BALANCE,
    ToolName.LIST_OVERDRAWN_PROJECTS,
    ToolName.EXPLAIN_RESOURCE_PAUSED_REASON,
    ToolName.EXPLAIN_INVOICE_COMPENSATIONS,
    ToolName.GET_CUSTOMER_CREDIT_OVERVIEW,
]

_MARKETPLACE_TOOLS: list[ToolName] = [
    ToolName.SEARCH_OFFERINGS,
    ToolName.GET_OFFERING,
    ToolName.LIST_CATEGORIES,
    ToolName.COMPARE_OFFERINGS,
]

_PROPOSALS_RESEARCHER_TOOLS: list[ToolName] = [
    ToolName.FIND_MATCHING_CALLS,
    ToolName.LIST_CALLS,
    ToolName.LIST_PROPOSALS,
    ToolName.GUIDE_PROPOSAL,
    ToolName.PROPOSAL_OVERVIEW,
]

# Reviewer tools split into two sub-groups: shared (any role assigned as a
# reviewer) and staff-only (call-management analytics).
_PROPOSALS_REVIEWER_TOOLS: list[ToolName] = [
    ToolName.REVIEW_WORKLOAD,
    ToolName.REVIEW_ASSISTANT,
]

_PROPOSALS_REVIEWER_STAFF_ONLY_TOOLS: list[ToolName] = [
    ToolName.CALL_INSIGHTS,
]

# Meta-tools — always available regardless of role. ``search_tools`` only
# returns specs for tools the user is already permitted to use; ``ask_user``
# is purely client-facing (collects user input) and grants no privileged
# data access.
_META_TOOLS: list[ToolName] = [ToolName.SEARCH_TOOLS, ToolName.ASK_USER]


STAFF_TOOLS: list[ToolName] = [
    *_VM_TOOLS,
    *_ACCOUNT_TOOLS,
    ToolName.GET_USER_OVERVIEW,
    *_MARKETPLACE_TOOLS,
    *_PROPOSALS_RESEARCHER_TOOLS,
    *_PROPOSALS_REVIEWER_TOOLS,
    *_PROPOSALS_REVIEWER_STAFF_ONLY_TOOLS,
    *_META_TOOLS,
]

SUPPORT_TOOLS: list[ToolName] = [
    *_VM_TOOLS,
    *_ACCOUNT_TOOLS,
    ToolName.GET_USER_OVERVIEW,
    *_MARKETPLACE_TOOLS,
    *_PROPOSALS_RESEARCHER_TOOLS,
    *_PROPOSALS_REVIEWER_TOOLS,
    *_META_TOOLS,
]

END_USER_TOOLS: list[ToolName] = [
    *_VM_TOOLS,
    *_ACCOUNT_TOOLS,
    *_MARKETPLACE_TOOLS,
    *_PROPOSALS_RESEARCHER_TOOLS,
    *_PROPOSALS_REVIEWER_TOOLS,
    *_META_TOOLS,
]

# Anonymous users (the public marketplace-discovery endpoint) get a fixed,
# read-only surface: marketplace browsing tools plus ``ask_user`` for
# clarifying questions. NO ``search_tools`` — anonymous flow uses a fixed
# tool set surfaced up-front rather than the lazy meta-tool loading the
# authenticated path uses, so the system prompt doesn't even need to
# mention ``search_tools`` exists.
ANONYMOUS_TOOLS: list[ToolName] = [
    *_MARKETPLACE_TOOLS,
    ToolName.ASK_USER,
]


def get_tool_set_for_user(user) -> list[ToolName]:
    """Return the tool list permitted for ``user``'s role.

    ``None`` and AnonymousUser both map to ``ANONYMOUS_TOOLS`` — the
    marketplace-discovery surface. Authenticated callers split by role:
    staff / support / end-user.

    Always returns a non-empty list — never ``None`` — so callers can
    treat the result as "the authoritative permitted set" without
    None-guards.
    """
    if user is None or getattr(user, "is_anonymous", False):
        return ANONYMOUS_TOOLS
    if user.is_staff:
        return STAFF_TOOLS
    if user.is_support:
        return SUPPORT_TOOLS
    return END_USER_TOOLS
