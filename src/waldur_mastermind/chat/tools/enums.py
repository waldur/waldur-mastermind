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
    PREVIEW_VM = "preview_vm"
    SHOW_USER_RESOURCES = "show_user_resources"
    LIST_PROJECTS = "list_projects"
