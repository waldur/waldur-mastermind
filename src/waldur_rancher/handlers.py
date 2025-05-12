import logging

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist

from keycloak import exceptions as keycloak_exceptions
from waldur_core.core.enums import CoreStates
from waldur_rancher.exceptions import RancherException

from . import backend, enums, models, utils

logger = logging.getLogger(__name__)


def delete_node_if_related_instance_has_been_deleted(sender, instance, **kwargs):
    try:
        node = models.Node.objects.get(instance=instance)
        backend = node.cluster.get_backend()
        backend.delete_node(node)
    except ObjectDoesNotExist:
        pass


def delete_cluster_if_all_related_nodes_have_been_deleted(sender, instance, **kwargs):
    node = instance
    try:
        if (
            node.cluster.state == CoreStates.DELETING
            and not node.cluster.node_set.count()
        ):
            backend = node.cluster.get_backend()
            backend.delete_cluster(node.cluster)
    except models.Cluster.DoesNotExist:
        logger.warning("Cluster instance has been removed already.")


def set_error_state_for_node_if_related_instance_deleting_is_failed(
    sender, instance, created=False, **kwargs
):
    if created:
        return

    try:
        node = models.Node.objects.get(instance=instance)
    except ObjectDoesNotExist:
        return

    if instance.tracker.has_changed("state") and instance.state == CoreStates.ERRED:
        node.state = CoreStates.ERRED
        node.error_message = "Deleting related VM has failed."
        node.save()


def set_error_state_for_cluster_if_related_node_deleting_is_failed(
    sender, instance, created=False, **kwargs
):
    node = instance

    if created:
        return

    if node.tracker.has_changed("state") and node.state == CoreStates.ERRED:
        if node.cluster.state == CoreStates.DELETING:
            node.cluster.state = CoreStates.ERRED
            node.cluster.error_message = "Deleting one or a more nodes have failed."
            node.cluster.save()


def delete_catalog_if_scope_has_been_deleted(sender, instance, **kwargs):
    content_type = ContentType.objects.get_for_model(instance)
    models.Catalog.objects.filter(
        object_id=instance.id, content_type=content_type
    ).delete()


def delete_keycloak_group_from_backend(sender, instance, **kwargs):
    group = instance
    try:
        _, settings = utils.get_keycloak_group_scope_and_settings(group)
        keycloak = backend.KeycloakBackend(settings)
        # Delete the group
        backend_group = keycloak.get_group(group.backend_id)
        if backend_group is None:
            # If the group is already removed in Keycloak, delete from DB
            group.delete()
            return
        keycloak.delete_group(group.backend_id)
        group.delete()
        # Delete the parent group if it has no subgroups
        group_parent_id = backend_group.get("parentId")
        if group_parent_id:
            backend_parent_group = keycloak.get_group(group_parent_id)
            if len(backend_parent_group["subGroups"]) == 0:
                keycloak.delete_group(group_parent_id)
    except keycloak_exceptions.KeycloakError as e:
        logger.error("Unable to delete the group %s in Keycloak: %s", group, e)


def delete_keycloak_user_group_membership_from_backend(sender, instance, **kwargs):
    try:
        group = instance.group
    except models.KeycloakGroup.DoesNotExist:
        # Skip removal if the group does not exist anymore
        return

    _, settings = utils.get_keycloak_group_scope_and_settings(group)
    try:
        keycloak = backend.KeycloakBackend(settings)
        backend_user = keycloak.find_user_by_username(instance.username)
        if backend_user is None:
            logger.info(
                "The user %s does not exist in Keycloak, skipping removal from group %s (%s)",
                instance.username,
                group.name,
                group.backend_id,
            )
            return
        remote_group = keycloak.get_group(group.backend_id)
        if remote_group is None:
            logger.info(
                "The group %s (%s) does not exist in Keycloak, skipping removal of user %s",
                group.name,
                group.backend_id,
                instance,
            )
            return
        keycloak.remove_user_from_group(backend_user["id"], group.backend_id)
        local_members = group.keycloakusergroupmembership_set.all()
        if len(local_members) == 0:
            # If the group has no local members, delete it from DB
            group.delete()
    except keycloak_exceptions.KeycloakError as e:
        logger.error("Unable to remove a user from the Keycloak group: %s", e)


def add_group_to_rancher_scope(sender, instance, created=False, **kwargs):
    if not created:
        return

    group = instance
    try:
        scope, settings = utils.get_keycloak_group_scope_and_settings(group)
        logger.info(
            "Adding group %s to Rancher %s %s", group, group.role.scope_type, scope.name
        )
        rancher_backend = backend.RancherBackend(settings)
        reference_group_name = f"keycloakoidc_group://{group.name}"
        if group.role.scope_type == enums.RoleScopeType.CLUSTER:
            rancher_backend.get_or_create_cluster_group_role(
                reference_group_name, scope.backend_id, group.role.name
            )
        elif group.role.scope_type == enums.RoleScopeType.PROJECT:
            rancher_backend.get_or_create_project_group_role(
                reference_group_name, scope.backend_id, group.role.name
            )
    except RancherException as e:
        logger.error(
            "Unable to add the group %s to Rancher %s %s: %s",
            group,
            group.role.scope_type,
            scope.name,
            e,
        )


def remove_group_from_rancher_scope(sender, instance, **kwargs):
    group = instance
    try:
        scope, settings = utils.get_keycloak_group_scope_and_settings(group)
        logger.info(
            "Removing group %s from Rancher %s %s",
            group,
            group.role.scope_type,
            scope.name,
        )
        rancher_backend = backend.RancherBackend(settings)
        reference_group_name = f"keycloakoidc_group://{group.name}"
        if group.role.scope_type == enums.RoleScopeType.CLUSTER:
            rancher_backend.delete_cluster_group_role(
                reference_group_name, scope.backend_id, group.role.name
            )
        elif group.role.scope_type == enums.RoleScopeType.PROJECT:
            rancher_backend.delete_project_group_role(
                reference_group_name, scope.backend_id, group.role.name
            )
    except RancherException as e:
        logger.error(
            "Unable to remove the group %s from Rancher %s %s: %s",
            group,
            group.role.scope_type,
            scope.name,
            e,
        )
