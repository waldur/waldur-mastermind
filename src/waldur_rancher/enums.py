from typing import Literal


class GlobalRoles:
    user_base = "user-base"


LONGHORN_NAME = "longhorn"


LONGHORN_NAMESPACE = "longhorn-system"


RANCHER_TEMPLATE_QUESTION_TYPE = ["boolean", "string", "enum", "secret"]

NodeRoleType = Literal["controlplane", "etcd", "worker"]

CatalogScopeType = Literal["global", "cluster", "project"]

CatalogScopeTypeChoices = ["global", "cluster", "project"]


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
