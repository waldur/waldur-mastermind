from typing import Literal


class GlobalRoles:
    user_base = "user-base"


RANCHER_TEMPLATE_QUESTION_TYPE = ["boolean", "string", "enum", "secret"]

NodeRoleType = Literal["agent", "server"]

CatalogScopeType = Literal["global", "cluster", "project"]

CatalogScopeTypeChoices = ["global", "cluster", "project"]


AGENT_ROLE = "agent"

SERVER_ROLE = "server"

ROLE_CHOICES = ((AGENT_ROLE, AGENT_ROLE), (SERVER_ROLE, SERVER_ROLE))


class RoleScopeType:
    CLUSTER = "cluster"
    PROJECT = "project"

    CHOICES = [
        (CLUSTER, "cluster"),
        (PROJECT, "project"),
    ]


class KeycloakUserGroupMembershipState:
    PENDING = "pending"
    ACTIVE = "active"

    CHOICES = (
        (PENDING, "pending"),
        (ACTIVE, "active"),
    )
