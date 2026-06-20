from enum import Enum


class ToolName(Enum):
    """Canonical chat tool identifiers.

    Used as the type for ``ToolDefinition.name`` and for tool set lists in
    ``tool_sets.py`` so that typos are caught at import/construction time
    rather than silently dropping a tool from a user's toolkit.

    Pydantic v2 coerces matching string values to the corresponding member
    at ``ToolDefinition`` construction, so tool files may pass either the
    enum member (preferred) or the raw string.
    """

    CREATE_VM = "create_vm"
    PLAN_VM = "plan_vm"
    DISPLAY_USER_RESOURCES = "display_user_resources"
    LIST_PROJECTS = "list_projects"
    LIST_ORGANIZATIONS = "list_organizations"
    GET_PROJECT_RESOURCES = "get_project_resources"
    GET_PROJECT_QUOTA = "get_project_quota"
    GET_RESOURCE_USAGE = "get_resource_usage"
    GET_USER_OVERVIEW = "get_user_overview"
    EXPLAIN_PROJECT_CREDIT_BALANCE = "explain_project_credit_balance"
    LIST_OVERDRAWN_PROJECTS = "list_overdrawn_projects"
    EXPLAIN_RESOURCE_PAUSED_REASON = "explain_resource_paused_reason"
    EXPLAIN_INVOICE_COMPENSATIONS = "explain_invoice_compensations"
    GET_CUSTOMER_CREDIT_OVERVIEW = "get_customer_credit_overview"
    FIND_MATCHING_CALLS = "find_matching_calls"
    LIST_CALLS = "list_calls"
    LIST_PROPOSALS = "list_proposals"
    GUIDE_PROPOSAL = "guide_proposal"
    REVIEW_WORKLOAD = "review_workload"
    CALL_INSIGHTS = "call_insights"
    PROPOSAL_OVERVIEW = "proposal_overview"
    REVIEW_ASSISTANT = "review_assistant"
    SEARCH_OFFERINGS = "search_offerings"
    GET_OFFERING = "get_offering"
    LIST_CATEGORIES = "list_categories"
    COMPARE_OFFERINGS = "compare_offerings"
    # Meta-tool: lazy-loads tool schemas by name to reduce system-prompt bloat.
    SEARCH_TOOLS = "search_tools"
    # Meta-tool: ask the user 1–4 structured multiple-choice questions when
    # required detail is missing and cannot be answered by another tool.
    ASK_USER = "ask_user"


class ToolCategory(str, Enum):
    """Functional grouping used by ``search_tools`` for batched fetches.

    The LLM sees these literal values as the JSON-Schema ``enum`` on
    ``search_tools.categories`` and in the system-prompt catalog as
    ``## <value>`` section headers. Meta-tools (e.g. ``search_tools``
    itself) opt out by leaving ``ToolDefinition.category`` at ``None``.
    """

    MARKETPLACE = "marketplace"
    VM = "vm"
    ACCOUNT = "account"
    PROPOSALS_RESEARCHER = "proposals_researcher"
    PROPOSALS_REVIEWER = "proposals_reviewer"
