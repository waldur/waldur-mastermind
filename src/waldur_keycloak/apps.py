from django.apps import AppConfig
from django.db.models import signals


class KeycloakConfig(AppConfig):
    name = "waldur_keycloak"
    verbose_name = "Keycloak"

    def ready(self):
        from waldur_core.core import models as core_models
        from waldur_core.permissions import signals as permission_signals
        from waldur_core.permissions.models import UserRole
        from waldur_mastermind.marketplace import models as marketplace_models

        from . import handlers, models

        signals.pre_delete.connect(
            handlers.mark_keycloak_group_deleting,
            sender=models.OfferingKeycloakGroup,
            dispatch_uid="waldur_keycloak.mark_keycloak_group_deleting",
        )

        signals.post_delete.connect(
            handlers.delete_keycloak_group_from_backend,
            sender=models.OfferingKeycloakGroup,
            dispatch_uid="waldur_keycloak.delete_keycloak_group_from_backend",
        )

        signals.post_delete.connect(
            handlers.delete_keycloak_membership_from_backend,
            sender=models.OfferingKeycloakMembership,
            dispatch_uid="waldur_keycloak.delete_keycloak_membership_from_backend",
        )

        signals.pre_delete.connect(
            handlers.cleanup_keycloak_groups_on_resource_delete,
            sender=marketplace_models.Resource,
            dispatch_uid="waldur_keycloak.cleanup_keycloak_groups_on_resource_delete",
        )

        signals.pre_delete.connect(
            handlers.cleanup_keycloak_groups_on_offering_delete,
            sender=marketplace_models.Offering,
            dispatch_uid="waldur_keycloak.cleanup_keycloak_groups_on_offering_delete",
        )

        # Gap #1: Clean up Keycloak memberships when user is deactivated
        signals.post_save.connect(
            handlers.cleanup_keycloak_on_user_deactivation,
            sender=core_models.User,
            dispatch_uid="waldur_keycloak.cleanup_keycloak_on_user_deactivation",
        )

        # Gap #3: Clean up Keycloak memberships when project role is revoked
        permission_signals.role_revoked.connect(
            handlers.cleanup_keycloak_on_role_revoked,
            sender=UserRole,
            dispatch_uid="waldur_keycloak.cleanup_keycloak_on_role_revoked",
        )
