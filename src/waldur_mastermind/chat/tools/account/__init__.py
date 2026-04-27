"""Account-scoped navigation tools for the AI Assistant.

Importing this subpackage triggers registration of each tool on
``tool_registry`` via the submodule side-effects listed below.
``apps.py`` imports this package once during ``ready()`` — individual
tool modules do not need to be listed there.
"""

from waldur_mastermind.chat.tools.account import (  # noqa: F401
    display_user_resources,
    get_project_quota,
    get_project_resources,
    get_resource_usage,
    get_user_overview,
    helpers,
    list_organizations,
    list_projects,
)
