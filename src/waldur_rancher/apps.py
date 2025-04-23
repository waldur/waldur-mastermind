from django.apps import AppConfig
from django.db.models import signals


class RancherConfig(AppConfig):
    name = "waldur_rancher"
    verbose_name = "Rancher"
    service_name = "Rancher"

    def ready(self):
        from waldur_core.structure import models as structure_models
        from waldur_core.structure.registry import SupportedServices
        from waldur_openstack.models import Instance

        from . import handlers, models
        from .backend import RancherBackend

        SupportedServices.register_backend(RancherBackend)

        signals.post_delete.connect(
            handlers.delete_node_if_related_instance_has_been_deleted,
            sender=Instance,
            dispatch_uid="waldur_rancher.delete_node_if_related_instance_has_been_deleted",
        )

        signals.post_delete.connect(
            handlers.delete_cluster_if_all_related_nodes_have_been_deleted,
            sender=models.Node,
            dispatch_uid="waldur_rancher.delete_cluster_if_all_related_nodes_have_been_deleted",
        )

        signals.post_save.connect(
            handlers.set_error_state_for_node_if_related_instance_deleting_is_failed,
            sender=Instance,
            dispatch_uid="waldur_rancher.set_error_state_for_node_if_related_instance_deleting_is_failed",
        )

        signals.post_save.connect(
            handlers.set_error_state_for_cluster_if_related_node_deleting_is_failed,
            sender=models.Node,
            dispatch_uid="waldur_rancher.set_error_state_for_cluster_if_related_node_deleting_is_failed",
        )

        signals.post_delete.connect(
            handlers.delete_keycloak_group_from_backend,
            sender=models.KeycloakGroup,
            dispatch_uid="waldur_rancher.delete_keycloak_group_from_backend",
        )

        signals.post_delete.connect(
            handlers.delete_keycloak_user_group_membership_from_backend,
            sender=models.KeycloakUserGroupMembership,
            dispatch_uid="waldur_rancher.delete_keycloak_user_group_membership_from_backend",
        )

        signals.post_save.connect(
            handlers.add_group_to_rancher_scope,
            sender=models.KeycloakGroup,
            dispatch_uid="waldur_rancher.add_group_to_rancher_scope",
        )

        signals.post_delete.connect(
            handlers.remove_group_from_rancher_scope,
            sender=models.KeycloakGroup,
            dispatch_uid="waldur_rancher.remove_group_from_rancher_scope",
        )

        for klass in (models.Project, models.Cluster, structure_models.ServiceSettings):
            signals.post_delete.connect(
                handlers.delete_catalog_if_scope_has_been_deleted,
                sender=klass,
                dispatch_uid="waldur_rancher.delete_catalog_if_scope_has_been_deleted_(%s)"
                % klass.__name__,
            )
