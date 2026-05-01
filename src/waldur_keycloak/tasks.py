import logging
import traceback

from celery import shared_task
from keycloak import exceptions as keycloak_exceptions

from waldur_mastermind.marketplace import models as marketplace_models

from . import models, utils
from .enums import KeycloakMembershipState

logger = logging.getLogger(__name__)


def _get_keycloak_offerings():
    """Return all offerings that have keycloak_enabled in plugin_options."""
    return marketplace_models.Offering.objects.filter(
        plugin_options__keycloak_enabled=True
    )


@shared_task(name="waldur_keycloak.sync_pending_memberships")
def sync_pending_memberships():
    """Synchronize pending Keycloak memberships by trying to add users to groups."""
    pending_memberships = models.OfferingKeycloakMembership.objects.filter(
        state=KeycloakMembershipState.PENDING
    )

    for membership in pending_memberships:
        try:
            group = membership.group
            offering = group.offering

            if not utils.is_keycloak_enabled(offering):
                continue

            keycloak = utils.get_keycloak_client_for_offering(offering)
            backend_user = keycloak.find_user_by_username(membership.username)

            if backend_user is None:
                logger.info(
                    "The user %s does not exist in Keycloak yet, "
                    "skipping adding user to the group %s (%s)",
                    membership.username,
                    group.name,
                    group.backend_id,
                )
            else:
                logger.info(
                    "Adding user %s to the group %s",
                    membership.username,
                    group.backend_id,
                )
                keycloak.add_user_to_group(backend_user["id"], group.backend_id)
                membership.first_name = backend_user.get("firstName", "")
                membership.last_name = backend_user.get("lastName", "")
                membership.activate()

            membership.error_message = ""
            membership.error_traceback = ""
            membership.refresh_last_checked()
            membership.save()

        except keycloak_exceptions.KeycloakError as e:
            logger.error(
                "Failed to assign role in Keycloak for user %s in group %s: %s",
                membership.username,
                membership.group.backend_id,
                e,
            )
            membership.error_message = (
                "Failed to sync membership with Keycloak. "
                "Contact your administrator if this persists."
            )
            membership.error_traceback = traceback.format_exc()
            membership.refresh_last_checked()
            membership.save()


@shared_task(name="waldur_keycloak.cleanup_orphaned_groups")
def cleanup_orphaned_groups():
    """Verify that Waldur-tracked Keycloak groups still exist remotely.

    Only inspects groups that Waldur manages (those with a backend_id).
    Does NOT delete remote groups that Waldur doesn't know about —
    they may be managed by other systems sharing the same Keycloak realm.

    If a remote group has been deleted externally, clears the local
    backend_id so the group can be re-linked or re-created.
    """
    for offering in _get_keycloak_offerings():
        try:
            keycloak = utils.get_keycloak_client_for_offering(offering)
            local_groups = models.OfferingKeycloakGroup.objects.filter(
                offering=offering
            ).exclude(backend_id="")

            for local_group in local_groups:
                try:
                    remote_group = keycloak.get_group(local_group.backend_id)
                    if remote_group is None:
                        logger.warning(
                            "Remote Keycloak group %s (backend_id=%s) no longer "
                            "exists for local group %s in offering %s. "
                            "Clearing backend_id.",
                            local_group.name,
                            local_group.backend_id,
                            local_group.uuid,
                            offering.uuid,
                        )
                        local_group.backend_id = ""
                        local_group.save(update_fields=["backend_id"])
                except keycloak_exceptions.KeycloakError as e:
                    logger.warning(
                        "Unable to verify remote group %s for offering %s: %s",
                        local_group.backend_id,
                        offering.uuid,
                        e,
                    )

        except (keycloak_exceptions.KeycloakError, ValueError) as e:
            logger.error(
                "Unable to check groups in Keycloak for offering %s: %s",
                offering.uuid,
                e,
            )


@shared_task(name="waldur_keycloak.cleanup_orphaned_memberships")
def cleanup_orphaned_memberships():
    """Verify that Waldur-tracked memberships still exist in remote Keycloak groups.

    Only inspects memberships that Waldur manages (those in groups with a backend_id).
    Does NOT remove remote users that Waldur doesn't know about —
    they may have been added by other systems sharing the same Keycloak realm.

    If a Waldur-tracked user has been removed from the remote group externally,
    marks the local membership with an error so administrators are aware.
    """
    for offering in _get_keycloak_offerings():
        try:
            keycloak = utils.get_keycloak_client_for_offering(offering)
            local_groups = models.OfferingKeycloakGroup.objects.filter(
                offering=offering
            ).exclude(backend_id="")

            for local_group in local_groups:
                try:
                    remote_members = keycloak.list_group_members(local_group.backend_id)
                    remote_usernames = {m.get("username", "") for m in remote_members}

                    # Check that each active local membership still exists remotely
                    active_memberships = (
                        models.OfferingKeycloakMembership.objects.filter(
                            group=local_group,
                            state=KeycloakMembershipState.ACTIVE,
                        )
                    )
                    for membership in active_memberships:
                        if membership.username not in remote_usernames:
                            logger.warning(
                                "User %s was removed from remote Keycloak "
                                "group %s (backend_id=%s) externally.",
                                membership.username,
                                local_group.name,
                                local_group.backend_id,
                            )
                            membership.error_message = (
                                "User was removed from the Keycloak group "
                                "externally. Re-add or delete this membership."
                            )
                            membership.refresh_last_checked()
                            membership.save(
                                update_fields=[
                                    "error_message",
                                    "last_checked",
                                ]
                            )

                except keycloak_exceptions.KeycloakError as e:
                    logger.warning(
                        "Unable to verify members of remote group %s "
                        "for offering %s: %s",
                        local_group.backend_id,
                        offering.uuid,
                        e,
                    )

        except (keycloak_exceptions.KeycloakError, ValueError) as e:
            logger.error(
                "Unable to check memberships in Keycloak for offering %s: %s",
                offering.uuid,
                e,
            )


@shared_task(name="waldur_keycloak.cleanup_keycloak_for_deactivated_user")
def cleanup_keycloak_for_deactivated_user(user_uuid):
    """Remove all Keycloak memberships for a deactivated user."""
    from waldur_core.core.models import User

    try:
        user = User.all_objects.get(uuid=user_uuid)
    except User.DoesNotExist:
        return

    # Double-check user is still inactive
    if user.is_active:
        return

    # Delete keycloak memberships
    models.OfferingKeycloakMembership.objects.filter(user=user).delete()


@shared_task(name="waldur_keycloak.cleanup_keycloak_for_lost_project_access")
def cleanup_keycloak_for_lost_project_access(user_uuid, project_uuid):
    """Remove Keycloak memberships for resources in a project
    the user no longer has access to."""
    from waldur_core.core.models import User
    from waldur_core.structure import models as structure_models
    from waldur_core.structure.managers import (
        get_connected_customers,
        get_connected_projects,
    )

    try:
        user = User.objects.get(uuid=user_uuid)
        project = structure_models.Project.objects.get(uuid=project_uuid)
    except (User.DoesNotExist, structure_models.Project.DoesNotExist):
        return

    if user.is_staff or user.is_support:
        return

    if not user.is_active:
        return  # Handled by deactivation task

    # Check if user still has direct project role
    connected_project_ids = set(get_connected_projects(user))
    if project.id in connected_project_ids:
        return

    # Check if user still has customer-level access (e.g. customer owner)
    connected_customer_ids = set(get_connected_customers(user))
    if project.customer_id in connected_customer_ids:
        return

    # Find resources in this project from keycloak-enabled offerings
    resources = marketplace_models.Resource.objects.filter(
        project=project,
        offering__plugin_options__keycloak_enabled=True,
    )

    if not resources.exists():
        return

    # Delete keycloak memberships for these resources
    models.OfferingKeycloakMembership.objects.filter(
        user=user,
        group__resource__in=resources,
    ).delete()
