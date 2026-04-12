from waldur_mastermind.chat.tools.enums import ToolName

STAFF_TOOLS: list[ToolName] = [
    ToolName.CREATE_VM,
    ToolName.PREVIEW_VM,
    ToolName.SHOW_USER_RESOURCES,
    ToolName.LIST_PROJECTS,
    ToolName.FIND_MATCHING_CALLS,
    ToolName.GUIDE_PROPOSAL,
    ToolName.REVIEW_WORKLOAD,
    ToolName.CALL_INSIGHTS,
    ToolName.PROPOSAL_OVERVIEW,
    ToolName.REVIEW_ASSISTANT,
]
SUPPORT_TOOLS: list[ToolName] = [
    ToolName.CREATE_VM,
    ToolName.PREVIEW_VM,
    ToolName.SHOW_USER_RESOURCES,
    ToolName.LIST_PROJECTS,
    ToolName.FIND_MATCHING_CALLS,
    ToolName.GUIDE_PROPOSAL,
    ToolName.REVIEW_WORKLOAD,
    ToolName.PROPOSAL_OVERVIEW,
    ToolName.REVIEW_ASSISTANT,
]
END_USER_TOOLS: list[ToolName] = [
    ToolName.CREATE_VM,
    ToolName.PREVIEW_VM,
    ToolName.SHOW_USER_RESOURCES,
    ToolName.LIST_PROJECTS,
    ToolName.FIND_MATCHING_CALLS,
    ToolName.GUIDE_PROPOSAL,
    ToolName.REVIEW_WORKLOAD,
    ToolName.PROPOSAL_OVERVIEW,
    ToolName.REVIEW_ASSISTANT,
]


def get_tool_set_for_user(user) -> list[ToolName] | None:
    """Return tool list based on user role. None means all tools."""
    if user is None:
        return None
    if user.is_staff:
        return STAFF_TOOLS
    if user.is_support:
        return SUPPORT_TOOLS
    return END_USER_TOOLS
