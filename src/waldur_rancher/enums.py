from django.db import models


class TemplateQuestionType(models.TextChoices):
    """Template question types for Rancher templates."""

    BOOLEAN = "boolean", "boolean"
    STRING = "string", "string"
    ENUM = "enum", "enum"
    SECRET = "secret", "secret"


class CatalogScopeType(models.TextChoices):
    """Catalog scope types for Rancher catalogs."""

    GLOBAL = "global", "global"
    CLUSTER = "cluster", "cluster"
    PROJECT = "project", "project"


class NodeRole(models.TextChoices):
    """Node role types for Rancher nodes."""

    AGENT = "agent", "agent"
    SERVER = "server", "server"


class RoleScopeType(models.TextChoices):
    """Role scope types for Rancher roles."""

    CLUSTER = "cluster", "cluster"
    PROJECT = "project", "project"


class KeycloakUserGroupMembershipState(models.TextChoices):
    """Keycloak user group membership states."""

    PENDING = "pending", "pending"
    ACTIVE = "active", "active"


class ClusterRuntimeStates:
    ACTIVE = "active"


class NodeRuntimeStates:
    ACTIVE = "active"
    REGISTERING = "registering"
    UNAVAILABLE = "unavailable"
