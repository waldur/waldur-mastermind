import logging
import threading

from django.db import transaction
from keycloak import exceptions as keycloak_exceptions

from . import signals, utils

logger = logging.getLogger(__name__)

# Thread-local state to prevent infinite loops during sync operations
_local = threading.local()


def is_syncing():
    return getattr(_local, "syncing", False)


def set_syncing(value):
    _local.syncing = value


def _get_deleting_groups():
    if not hasattr(_local, "deleting_groups"):
        _local.deleting_groups = set()
    return _local.deleting_groups


def mark_keycloak_group_deleting(sender, instance, **kwargs):
    """Mark a group as being deleted so cascade membership handlers skip re-deletion."""
    _get_deleting_groups().add(instance.pk)


def delete_keycloak_group_from_backend(sender, instance, **kwargs):
    """Delete a Keycloak group from the backend when the local model is deleted."""
    from . import models

    group = instance
    _get_deleting_groups().discard(group.pk)

    offering = group.offering
    if not utils.is_keycloak_enabled(offering):
        return

    try:
        resource = group.resource
    except Exception:
        resource = None
    signals.keycloak_group_deleting.send(
        sender=models.OfferingKeycloakGroup,
        group=group,
        offering=offering,
        resource=resource,
    )

    if not group.backend_id:
        return

    try:
        keycloak = utils.get_keycloak_client_for_offering(offering)
        backend_group = keycloak.get_group(group.backend_id)
        if backend_group is None:
            return
        keycloak.delete_group(group.backend_id)
    except keycloak_exceptions.KeycloakError as e:
        logger.error("Unable to delete the group %s in Keycloak: %s", group, e)


def delete_keycloak_membership_from_backend(sender, instance, **kwargs):
    """Remove a user from a Keycloak group when membership is deleted.
    Also deletes the group if it has no remaining memberships."""
    from . import models

    try:
        group = instance.group
    except models.OfferingKeycloakGroup.DoesNotExist:
        return

    offering = group.offering
    if not utils.is_keycloak_enabled(offering):
        return

    if not group.backend_id:
        return

    try:
        keycloak = utils.get_keycloak_client_for_offering(offering)
        backend_user = keycloak.find_user_by_username(instance.username)
        if backend_user is None:
            logger.info(
                "The user %s does not exist in Keycloak, "
                "skipping removal from group %s (%s)",
                instance.username,
                group.name,
                group.backend_id,
            )
            return
        remote_group = keycloak.get_group(group.backend_id)
        if remote_group is None:
            logger.info(
                "The group %s (%s) does not exist in Keycloak, "
                "skipping removal of user %s",
                group.name,
                group.backend_id,
                instance,
            )
            return
        keycloak.remove_user_from_group(backend_user["id"], group.backend_id)

        # Delete the group if no remaining memberships.
        # Skip if the group is already being deleted (cascade).
        deleting = _get_deleting_groups()
        if group.pk not in deleting:
            remaining = group.memberships.exclude(pk=instance.pk).exists()
            if not remaining:
                group.delete()

    except keycloak_exceptions.KeycloakError as e:
        logger.error("Unable to remove a user from the Keycloak group: %s", e)


def sync_resource_user_to_keycloak_membership(
    sender, instance, created=False, **kwargs
):
    """When a ResourceUser is created, auto-create a Keycloak membership
    if the resource's offering has keycloak_enabled."""
    if is_syncing():
        return

    if not created:
        return

    from . import models

    resource_user = instance
    offering = resource_user.resource.offering

    if not utils.is_keycloak_enabled(offering):
        return

    user = resource_user.user
    role = resource_user.role

    # Check if membership already exists
    existing = models.OfferingKeycloakMembership.objects.filter(
        group__offering=offering,
        group__role=role,
        group__resource=resource_user.resource,
        user=user,
    ).exists()

    if existing:
        return

    # Get or create the keycloak group
    group, _ = models.OfferingKeycloakGroup.objects.get_or_create(
        offering=offering,
        role=role,
        resource=resource_user.resource,
        scope_id="",
        defaults={
            "name": utils.get_keycloak_group_name(
                offering, role, resource_user.resource
            ),
        },
    )

    set_syncing(True)
    try:
        models.OfferingKeycloakMembership.objects.get_or_create(
            username=user.username,
            group=group,
            defaults={
                "email": user.email,
                "user": user,
            },
        )
    finally:
        set_syncing(False)


def delete_keycloak_membership_on_resource_user_delete(sender, instance, **kwargs):
    """When a ResourceUser is deleted, delete corresponding Keycloak membership."""
    if is_syncing():
        return

    from . import models

    resource_user = instance
    offering = resource_user.resource.offering

    if not utils.is_keycloak_enabled(offering):
        return

    set_syncing(True)
    try:
        models.OfferingKeycloakMembership.objects.filter(
            group__offering=offering,
            group__role=resource_user.role,
            group__resource=resource_user.resource,
            user=resource_user.user,
        ).delete()
    finally:
        set_syncing(False)


def cleanup_keycloak_groups_on_resource_delete(sender, instance, **kwargs):
    """When a marketplace Resource is deleted, delete all its Keycloak groups."""
    from . import models

    resource = instance
    offering = resource.offering

    if not utils.is_keycloak_enabled(offering):
        return

    models.OfferingKeycloakGroup.objects.filter(resource=resource).delete()


def cleanup_keycloak_groups_on_offering_delete(sender, instance, **kwargs):
    """When a marketplace Offering is deleted, delete all its Keycloak groups."""
    from . import models

    offering = instance

    if not utils.is_keycloak_enabled(offering):
        return

    models.OfferingKeycloakGroup.objects.filter(offering=offering).delete()


def cleanup_keycloak_on_user_deactivation(sender, instance, **kwargs):
    """When a user is deactivated, schedule cleanup of all their Keycloak memberships."""
    from . import models

    user = instance
    if user.is_active:
        return

    # Only act on saves that include is_active change (or full saves)
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and "is_active" not in update_fields:
        return

    # Quick check: does the user have any keycloak memberships?
    if not models.OfferingKeycloakMembership.objects.filter(user=user).exists():
        return

    from . import tasks

    transaction.on_commit(
        lambda: tasks.cleanup_keycloak_for_deactivated_user.delay(user.uuid.hex)
    )


def cleanup_keycloak_on_role_revoked(sender, instance, **kwargs):
    """When a project role is revoked, schedule cleanup of Keycloak memberships
    for resources in that project if the user no longer has access."""
    from waldur_core.structure import models as structure_models

    user_role = instance
    if not isinstance(user_role.scope, structure_models.Project):
        return

    from . import tasks

    user_uuid = user_role.user.uuid.hex
    project_uuid = user_role.scope.uuid.hex

    transaction.on_commit(
        lambda: tasks.cleanup_keycloak_for_lost_project_access.delay(
            user_uuid,
            project_uuid,
        )
    )
