import logging
import re
import time
import traceback
from typing import cast

import yaml
from celery import shared_task
from keycloak import exceptions as keycloak_exceptions
from rest_framework import status
from rest_framework.reverse import reverse

from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.core.exceptions import RuntimeStateException
from waldur_core.core.models import User
from waldur_core.structure import models as structure_models
from waldur_core.structure.signals import resource_imported
from waldur_kubernetes.backend import KubernetesBackend
from waldur_mastermind.common import utils as common_utils
from waldur_openstack import models as openstack_models
from waldur_openstack.views import MarketplaceInstanceViewSet
from waldur_rancher.enums import (
    KeycloakUserGroupMembershipState,
)

from . import backend, enums, exceptions, models, utils

logger = logging.getLogger(__name__)


class CreateNodeTask(core_tasks.Task):
    def get_vault_temp_credentials(
        self, node, vault_host, vault_port, vault_token, vault_tls_verify
    ):
        vault_backend = backend.VaultBackend(
            vault_host, vault_port, vault_token, vault_tls_verify
        )
        role_name = f"rancher-provisioning-role-{node.cluster.uuid.hex}"
        role_id = vault_backend.get_role_id(role_name)
        role_secret_id = vault_backend.generate_role_secret_id(role_name)
        return role_id, role_secret_id

    def execute(self, instance: models.Node, user_id):
        node = instance
        flavor = node.initial_data["flavor"]
        system_volume_size = node.initial_data["system_volume_size"]
        system_volume_type = node.initial_data.get("system_volume_type")
        data_volumes = node.initial_data.get("data_volumes", [])
        image = node.initial_data["image"]
        subnet = node.initial_data["subnet"]
        security_groups = node.initial_data["security_groups"]
        service_settings: str = node.initial_data["service_settings"]
        tenant: str = node.initial_data["tenant"]
        project: str = node.initial_data["project"]
        user = User.objects.get(pk=user_id)
        ssh_public_key = node.initial_data.get("ssh_public_key")

        cloud_init_extra_params = {}
        if node.cluster.service_settings.get_option("vault_host"):
            vault_host = node.cluster.service_settings.get_option("vault_host")
            vault_port = node.cluster.service_settings.get_option("vault_port")
            vault_token = node.cluster.service_settings.get_option("vault_token")
            vault_tls_verify_raw = node.cluster.service_settings.get_option(
                "vault_tls_verify"
            )
            vault_tls_verify = (
                vault_tls_verify_raw if vault_tls_verify_raw is not None else True
            )
            role_id, role_secret_id = self.get_vault_temp_credentials(
                instance,
                vault_host,
                vault_port,
                vault_token,
                vault_tls_verify,
            )
            cloud_init_extra_params.update(
                {
                    "vault_secret_path": f"secret/rancher/cluster-{node.cluster.uuid.hex}",
                    "vault_role_id": role_id,
                    "vault_role_secret_id": role_secret_id,
                    "vault_addr": f"https://{vault_host}:{vault_port}",
                    "rke_role": node.role,
                }
            )

        post_data = {
            "name": node.name,
            "flavor": reverse("openstack-flavor-detail", kwargs={"uuid": flavor}),
            "image": reverse("openstack-image-detail", kwargs={"uuid": image}),
            "service_settings": reverse(
                "servicesettings-detail", kwargs={"uuid": service_settings}
            ),
            "tenant": reverse("openstack-tenant-detail", kwargs={"uuid": tenant}),
            "project": reverse("project-detail", kwargs={"uuid": project}),
            "system_volume_size": system_volume_size,
            "system_volume_type": system_volume_type
            and reverse(
                "openstack-volume-type-detail",
                kwargs={"uuid": system_volume_type},
            ),
            "data_volumes": [
                {
                    "size": volume["size"],
                    "volume_type": volume.get("volume_type")
                    and reverse(
                        "openstack-volume-type-detail",
                        kwargs={"uuid": volume.get("volume_type")},
                    ),
                }
                for volume in data_volumes
            ],
            "security_groups": [
                {"url": reverse("openstack-sgp-detail", kwargs={"uuid": group})}
                for group in security_groups
            ],
            "ports": [
                {"subnet": reverse("openstack-subnet-detail", kwargs={"uuid": subnet})}
            ],
            "user_data": utils.format_node_cloud_config(node, cloud_init_extra_params),
        }

        if node.cluster.settings.get_option("allocate_floating_ip_to_all_nodes"):
            post_data["floating_ips"] = [
                {"subnet": reverse("openstack-subnet-detail", kwargs={"uuid": subnet})}
            ]

        if ssh_public_key:
            post_data["ssh_public_key"] = reverse(
                "sshpublickey-detail",
                kwargs={"uuid": ssh_public_key},
            )

        view = MarketplaceInstanceViewSet.as_view({"post": "create"})
        response = common_utils.create_request(view, user, post_data)

        if response.status_code != status.HTTP_201_CREATED:
            raise exceptions.RancherException(response.data)

        data = cast(dict, response.data)
        instance_uuid = data["uuid"]
        vm = openstack_models.Instance.objects.get(uuid=instance_uuid)
        node.instance = vm
        node.state = CoreStates.CREATING
        node.save()

        resource_imported.send(
            sender=vm.__class__,
            instance=vm,
        )

    @classmethod
    def get_description(cls, instance, *args, **kwargs):
        return f'Create nodes for k8s cluster "{instance}".'


class DeleteNodeTask(core_tasks.Task):
    def execute(self, instance: models.Node, user_id: str):
        node = instance
        user = User.objects.get(pk=user_id)
        vm = node.instance

        backend = node.get_backend()
        backend.drain_node(node)

        timeout = 60  # seconds
        start_time = time.time()
        node_drain_status = None
        while True:
            node_drain_status = backend.get_node_drain_status(node)
            if node_drain_status == "ok":
                break
            elif node_drain_status == "error":
                raise exceptions.RancherException("Node drain failed.")

            if time.time() - start_time > timeout:
                raise exceptions.RancherException("Node drain timeout.")

            time.sleep(5)

        if vm:
            view = MarketplaceInstanceViewSet.as_view({"delete": "force_destroy"})
            response = common_utils.delete_request(
                view,
                user,
                uuid=vm.uuid.hex,
                query_params={"delete_volumes": True},
            )

            if response.status_code != status.HTTP_202_ACCEPTED:
                raise exceptions.RancherException(response.data)
        else:
            backend = node.get_backend()
            backend.delete_node(node)


@shared_task
def pull_cluster_nodes(cluster_id):
    cluster = models.Cluster.objects.get(id=cluster_id)
    backend = cluster.get_backend()

    if cluster.node_set.filter(backend_id="").exists():
        backend_nodes = backend.get_cluster_nodes(cluster.backend_id)

        for backend_node in backend_nodes:
            if cluster.node_set.filter(name=backend_node["name"]).exists():
                node = cluster.node_set.get(name=backend_node["name"])
                node.backend_id = backend_node["backend_id"]
                node.save()

    for node in cluster.node_set.exclude(backend_id=""):
        backend.pull_node(node)
        node.refresh_from_db()


@shared_task(name="waldur_rancher.pull_all_clusters_nodes")
def pull_all_clusters_nodes():
    """Pull node information for all Rancher clusters and update their states."""
    for cluster in models.Cluster.objects.exclude(backend_id=""):
        pull_cluster_nodes(cluster.id)
        utils.update_cluster_nodes_states(cluster.id)


class PollRuntimeStateNodeTask(core_tasks.Task):
    max_retries = 600
    default_retry_delay = 10

    @classmethod
    def get_description(cls, node, *args, **kwargs):
        node = cast(models.Node, core_utils.deserialize_instance(node))
        return f'Poll node "{node.name}"'

    def execute(self, node: models.Node):
        pull_cluster_nodes(node.cluster_id)
        node.refresh_from_db()

        if node.runtime_state == models.Node.RuntimeStates.ACTIVE:
            # We don't need to change the node state here as it will be done
            # in an executor.
            return
        elif (
            node.runtime_state
            in [
                models.Node.RuntimeStates.REGISTERING,
                models.Node.RuntimeStates.UNAVAILABLE,
            ]
            or not node.runtime_state
        ):
            self.retry()
        elif node.runtime_state:
            raise RuntimeStateException(
                f"{node.__class__.__name__} (PK: {node.pk}) runtime state become erred: {node.runtime_state}"
            )

        return node


class CreateVaultCredentialsTask(core_tasks.Task):
    @classmethod
    def get_description(cls, cluster, *args, **kwargs):
        cluster = core_utils.deserialize_instance(cluster)
        return f"Create secret and temporary secret id for it in Vault for cluster {cluster}"

    def execute(self, cluster: models.Cluster, *args, **kwargs):
        policy_name = f"rancher-provisioning-policy-{cluster.uuid.hex}"
        role_name = f"rancher-provisioning-role-{cluster.uuid.hex}"
        secret_name = f"rancher/cluster-{cluster.uuid.hex}"

        vault_host = cluster.service_settings.get_option("vault_host")
        vault_port = cluster.service_settings.get_option("vault_port")
        vault_token = cluster.service_settings.get_option("vault_token")
        vault_tls_verify_raw = cluster.service_settings.get_option("vault_tls_verify")
        vault_tls_verify = (
            vault_tls_verify_raw if vault_tls_verify_raw is not None else True
        )
        if not vault_host:
            raise exceptions.RancherException(
                "Unable to get vault host from the cluster settings"
            )
        if not vault_port:
            raise exceptions.RancherException(
                "Unable to get vault port from the cluster settings"
            )
        if not vault_token:
            raise exceptions.RancherException(
                "Unable to get vault token from the cluster settings"
            )
        vault_backend = backend.VaultBackend(
            vault_host, vault_port, vault_token, vault_tls_verify
        )
        policy_body = {
            "path": {
                f"secret/data/rancher/cluster-{cluster.uuid.hex}": {
                    "capabilities": ["read"],
                },
            },
        }
        vault_backend.create_or_update_policy(policy_name, policy_body)
        role_params = {
            "secret_id_ttl": "60m",
            "secret_id_num_uses": 1,
            "token_num_uses": 1,
            "token_ttl": "60m",
            "token_max_ttl": "60m",
        }
        vault_backend.create_or_update_role(role_name, policy_name, role_params)

        # Fetch the cluster registration token and put it into the Vault secret
        rancher_backend: backend.RancherBackend = cluster.get_backend()
        all_tokens = rancher_backend.client.list_cluster_registration_tokens()
        cluster_tokens = [
            token for token in all_tokens if token["clusterId"] == cluster.backend_id
        ]
        token = cluster_tokens[0]["token"]

        vault_backend.create_or_update_secret(secret_name, {"token": token})


class DeleteVaultObjectsTask(core_tasks.Task):
    @classmethod
    def get_description(cls, cluster, *args, **kwargs):
        cluster = core_utils.deserialize_instance(cluster)
        return (
            f"Remove Vault objects for the cluster {cluster} (secrets, policies, etc.)"
        )

    def execute(self, cluster: models.Cluster, *args, **kwargs):
        policy_name = f"rancher-provisioning-policy-{cluster.uuid.hex}"
        role_name = f"rancher-provisioning-role-{cluster.uuid.hex}"
        secret_name = f"rancher/cluster-{cluster.uuid.hex}"

        vault_host = cluster.service_settings.get_option("vault_host")
        vault_port = cluster.service_settings.get_option("vault_port")
        vault_token = cluster.service_settings.get_option("vault_token")
        vault_tls_verify_raw = cluster.service_settings.get_option("vault_tls_verify")
        vault_tls_verify = (
            vault_tls_verify_raw if vault_tls_verify_raw is not None else True
        )
        if not vault_host:
            raise exceptions.RancherException(
                "Unable to get vault host from the cluster settings"
            )
        if not vault_port:
            raise exceptions.RancherException(
                "Unable to get vault port from the cluster settings"
            )
        if not vault_token:
            raise exceptions.RancherException(
                "Unable to get vault token from the cluster settings"
            )
        vault_backend = backend.VaultBackend(
            vault_host, vault_port, vault_token, vault_tls_verify
        )

        vault_backend.delete_policy(policy_name)
        vault_backend.delete_role(role_name)
        vault_backend.delete_secret(secret_name)


class CreateArgoCDClusterSecretTask(core_tasks.Task):
    @classmethod
    def get_description(cls, cluster, *args, **kwargs):
        cluster = core_utils.deserialize_instance(cluster)
        return f"Create an ArgoCD cluster secret for cluster {cluster}"

    def execute(self, instance: models.Cluster, *args, **kwargs):
        # Lazy import: keep the kubernetes SDK out of Django startup (autodiscover
        # imports this tasks module). See CLAUDE.md, "Lazy imports for heavy
        # optional backends".
        import kubernetes

        install_longhorn = kwargs.get("install_longhorn", False)
        kubeconfig_str = instance.settings.get_option("argocd_k8s_kubeconfig")
        if not kubeconfig_str:
            logger.warning(
                "Unable to get argocd kubeconfig file from the %s cluster settings, skipping ArgoCD secret creation"
            )
            return

        k8s = KubernetesBackend(kubeconfig_str=kubeconfig_str)
        secret_name = f"cluster-{instance.uuid.hex}"
        argocd_namespace = instance.settings.get_option("argocd_k8s_namespace")
        cluster_backend: backend.RancherBackend = instance.get_backend()
        cluster_kubeconfig_str = cluster_backend.get_kubeconfig_file(instance)
        cluster_kubecofnig = yaml.safe_load(cluster_kubeconfig_str)
        cluster_info = cluster_kubecofnig["clusters"][0]
        user_info = cluster_kubecofnig["users"][0]
        secret_data = {
            "name": cluster_info["name"],
            "server": cluster_info["cluster"]["server"],
            "config": """
                {
                    "bearerToken": "%s"
                }
            """
            % user_info["user"]["token"],
        }
        if not argocd_namespace:
            raise exceptions.RancherException(
                "Unable to get argocd namespace from the cluster settings"
            )
        secret_labels = {"argocd.argoproj.io/secret-type": "cluster"}
        if install_longhorn:
            secret_labels["k8s.waldur.com/longhorn-enabled"] = "true"
        try:
            k8s.create_k8s_secret(
                secret_name,
                argocd_namespace,
                string_data=secret_data,
                labels=secret_labels,
            )
        except kubernetes.client.ApiException as e:
            logger.error(
                "Unable to create an ArgoCD secret for cluster %s, reason: %s",
                instance,
                e,
            )


class DeleteKeycloakGroupsTask(core_tasks.Task):
    @classmethod
    def get_description(cls, cluster, *args, **kwargs):
        cluster = core_utils.deserialize_instance(cluster)
        return f"Delete Keycloak objects for the cluster {cluster}"

    def execute(self, cluster: models.Cluster, *args, **kwargs):
        settings = cluster.settings
        cluster_groups = models.KeycloakGroup.objects.filter(
            scope_uuid=cluster.uuid,
            role__scope_type=enums.RoleScopeType.CLUSTER,
            role__settings=settings,
        )
        project_uuids = models.Project.objects.filter(cluster=cluster).values_list(
            "uuid", flat=True
        )
        project_groups = models.KeycloakGroup.objects.filter(
            scope_uuid__in=project_uuids,
            role__scope_type=enums.RoleScopeType.PROJECT,
            role__settings=settings,
        )
        # Deleting only DB records, while handlers delete the groups in the Keycloak backend
        logger.info(
            "Deleting %s Keycloak groups for cluster %s",
            cluster_groups.count(),
            cluster.name,
        )
        cluster_groups.delete()
        logger.info(
            "Deleting %s Keycloak groups for cluster %s projects",
            cluster_groups.count(),
            cluster.name,
        )
        project_groups.delete()


@shared_task(name="waldur_rancher.sync_keycloak_users")
def sync_keycloak_users():
    """Synchronize Keycloak users with pending group memberships in Rancher."""
    pending_users_memberships = models.KeycloakUserGroupMembership.objects.filter(
        state=KeycloakUserGroupMembershipState.PENDING
    )

    for user_membership in pending_users_memberships:
        try:
            group = user_membership.group
            _, settings = utils.get_keycloak_group_scope_and_settings(group)
            keycloak = backend.KeycloakBackend(settings)
            backend_user = keycloak.find_user_by_username(user_membership.username)
            if backend_user is None:
                logger.info(
                    "The user %s does not exist in Keycloak yet, skipping adding user to the group %s (%s)",
                    user_membership.username,
                    group.name,
                    group.backend_id,
                )
            else:
                logger.info(
                    "Adding user %s to the group %s",
                    user_membership.username,
                    group.backend_id,
                )
                keycloak.add_user_to_group(backend_user["id"], group.backend_id)
                user_membership.first_name = backend_user["firstName"]
                user_membership.last_name = backend_user["lastName"]
                user_membership.activate()
            user_membership.error_message = ""
            user_membership.error_traceback = ""
            user_membership.refresh_last_checked()
            user_membership.save()
        except keycloak_exceptions.KeycloakError as e:
            # Log error but keep as pending to retry
            logger.error(
                "Failed to assign role in Keycloak for user %s in group %s: %s",
                user_membership.username,
                user_membership.group.backend_id,
                e,
            )
            user_membership.error_message = str(e)
            user_membership.error_traceback = traceback.format_exc()
            user_membership.refresh_last_checked()
            user_membership.save()


@shared_task(name="waldur_rancher.sync_rancher_roles")
def sync_rancher_roles():
    """Synchronize Rancher roles with local role templates for clusters and projects."""

    def create_role(remote_role, scope_type, settings):
        logger.info(
            "Creating new %s role %s for Rancher %s",
            scope_type,
            remote_role["id"],
            settings.backend_url,
        )
        role = models.RoleTemplate(
            name=remote_role["id"],
            display_name=remote_role["name"],
            scope_type=scope_type,
            settings=settings,
        )
        role.save()

    def sync_roles(
        remote_roles_all: list[dict],
        scope_type: enums.CatalogScopeType,
        settings: structure_models.ServiceSettings,
    ):
        # Collecting roles
        remote_roles = {
            role["id"]: role
            for role in remote_roles_all
            if role["context"] == scope_type
        }
        local_roles = models.RoleTemplate.objects.filter(
            settings=settings, scope_type=scope_type
        )
        # Remove local stale roles
        stale_roles = local_roles.exclude(name__in=remote_roles.keys())
        logger.info(
            "Removing %s %s roles for Rancher %s",
            stale_roles.count(),
            scope_type,
            settings.backend_url,
        )
        stale_roles.delete()
        # Create new roles
        local_role_names = local_roles.values_list("name", flat=True)
        new_roles = [
            remote_role
            for remote_role_name, remote_role in remote_roles.items()
            if remote_role_name not in local_role_names
        ]
        for new_role in new_roles:
            create_role(new_role, scope_type, settings)
        # Update the existing roles
        existing_roles = local_roles.filter(name__in=remote_roles.keys())
        for existing_role in existing_roles:
            existing_role.display_name = remote_roles[existing_role.name]["name"]
            existing_role.save(update_fields=["display_name"])

    clusters = models.Cluster.objects.filter(state=CoreStates.OK)
    rancher_settings_ids = (
        clusters.order_by().values_list("settings", flat=True).distinct()
    )
    for rancher_settings_id in rancher_settings_ids:
        settings = structure_models.ServiceSettings.objects.get(id=rancher_settings_id)
        try:
            rancher_backend = backend.RancherBackend(settings)
            # Collecting remote roles
            remote_roles = rancher_backend.list_roles()
            # Syncing roles
            sync_roles(remote_roles, enums.RoleScopeType.CLUSTER, settings)
            sync_roles(remote_roles, enums.RoleScopeType.PROJECT, settings)
        except exceptions.RancherException as e:
            logger.error(
                "Unable to sync roles for rancher cluster %s, reason: %s",
                settings.backend_url,
                e,
            )


@shared_task(name="waldur_rancher.delete_leftover_keycloak_groups")
def delete_leftover_keycloak_groups():
    """
    Delete remote Keycloak groups with no linked groups in Waldur
    """
    clusters = models.Cluster.objects.filter(state=CoreStates.OK)
    rancher_settings_ids = (
        clusters.order_by().values_list("settings", flat=True).distinct()
    )
    for rancher_settings_id in rancher_settings_ids:
        settings = structure_models.ServiceSettings.objects.get(id=rancher_settings_id)
        try:
            local_groups = models.KeycloakGroup.objects.filter(role__settings=settings)
            local_group_ids = local_groups.values_list("backend_id", flat=True)
            keycloak = backend.KeycloakBackend(settings)
            # Get all groups and filter out only parent ones created by Waldur
            # For this, use name matching to format: "c_<CLUSTER_UUID>"
            remote_all_parent_groups = keycloak.list_groups()
            remote_parent_groups = [
                group
                for group in remote_all_parent_groups
                if re.match(r"^[cp]_[0-9a-f]{32}$", group["name"], re.IGNORECASE)
            ]
            for remote_parent_group in remote_parent_groups:
                # Get all the subgroups with no linked groups in Waldur and with names matching the patterns: "c_<CLUSTER_UUID>_<ROLE_NAME>" or "p_<PROJECT_UUID>_<ROLE_NAME>"
                remote_groups = remote_parent_group.get("subGroups", [])
                remote_stale_groups = [
                    group
                    for group in remote_groups
                    if group["id"] not in local_group_ids
                    and re.match(
                        r"^[cp]_[0-9a-f]{32}_.*$", group["name"], re.IGNORECASE
                    )
                ]
                for remote_stale_group in remote_stale_groups:
                    keycloak.delete_group(remote_stale_group["id"])
                parent_group_id = remote_parent_group["id"]
                # Refresh local parent group cache and delete the group if no children left
                parent_group = keycloak.get_group(parent_group_id)
                if len(parent_group.get("subGroups", [])) == 0:
                    keycloak.delete_group(parent_group_id)
        except keycloak_exceptions.KeycloakError as e:
            logger.info("Unable to delete leftover groups in Keycloak, reason: %s", e)


@shared_task(name="waldur_rancher.delete_leftover_keycloak_memberships")
def delete_leftover_keycloak_memberships():
    """
    Delete remote Keycloak user memberships in groups with no linked instances in Waldur
    """
    clusters = models.Cluster.objects.filter(state=CoreStates.OK)
    rancher_settings_ids = (
        clusters.order_by().values_list("settings", flat=True).distinct()
    )
    for rancher_settings_id in rancher_settings_ids:
        settings = structure_models.ServiceSettings.objects.get(id=rancher_settings_id)
        try:
            keycloak = backend.KeycloakBackend(settings)
            local_groups = models.KeycloakGroup.objects.filter(role__settings=settings)
            for local_group in local_groups:
                local_member_usernames = (
                    models.KeycloakUserGroupMembership.objects.filter(
                        group=local_group
                    ).values_list("username", flat=True)
                )
                remote_members = keycloak.list_group_members(local_group.backend_id)
                remote_stale_member_ids = [
                    member["id"]
                    for member in remote_members
                    if member["username"] not in local_member_usernames
                ]
                for remote_state_member_id in remote_stale_member_ids:
                    keycloak.remove_user_from_group(
                        remote_state_member_id, local_group.backend_id
                    )
        except keycloak_exceptions.KeycloakError as e:
            logger.info(
                "Unable to delete leftover memberships in Keycloak, reason: %s", e
            )


@shared_task(name="waldur_rancher.sync_rancher_group_bindings")
def sync_rancher_group_bindings():
    """
    Sync group bindings in Rancher with the groups in Waldur.
    """

    def create_missing_cluster_groups(
        rancher_backend: backend.RancherBackend,
        cluster: models.Cluster,
        local_cluster_groups: list[models.KeycloakGroup],
    ):
        for local_cluster_group in local_cluster_groups:
            try:
                reference_group_name = (
                    f"keycloakoidc_group://{local_cluster_group.name}"
                )
                rancher_backend.get_or_create_cluster_group_role(
                    reference_group_name,
                    cluster.backend_id,
                    local_cluster_group.role.name,
                )
            except exceptions.RancherException as e:
                logger.error(
                    "Unable to sync group %s binding for cluster %s, reason: %s",
                    local_cluster_group.name,
                    cluster.name,
                    e,
                )

    def remove_stale_cluster_groups(
        rancher_backend: backend.RancherBackend,
        cluster: models.Cluster,
        local_cluster_groups: list[models.KeycloakGroup],
    ):
        remote_cluster_groups_all = rancher_backend.client.get_cluster_group_role(
            cluster_id=cluster.backend_id
        )
        remote_cluster_groups_filtered = [
            item
            for item in remote_cluster_groups_all
            if item["groupPrincipalId"] is not None
            and "keycloakoidc_group://" in item["groupPrincipalId"]
        ]
        remote_cluster_groups = {
            group["groupPrincipalId"]: group for group in remote_cluster_groups_filtered
        }
        local_cluster_group_principal_names = [
            f"keycloakoidc_group://{group.name}" for group in local_cluster_groups
        ]
        remote_stale_cluster_group_ids = (
            remote_cluster_groups.keys() - local_cluster_group_principal_names
        )
        for remote_stale_group_id in remote_stale_cluster_group_ids:
            try:
                remote_group = remote_cluster_groups[remote_stale_group_id]
                rancher_backend.delete_cluster_group_role(
                    remote_stale_group_id,
                    cluster.backend_id,
                    remote_group["roleTemplateId"],
                )
            except exceptions.RancherException as e:
                logger.error(
                    "Unable to delete group %s binding for cluster %s, reason: %s",
                    remote_stale_group_id,
                    cluster.name,
                    e,
                )

    def create_missing_project_groups(
        rancher_backend: backend.RancherBackend,
        project: models.Project,
        local_project_groups: list[models.KeycloakGroup],
    ):
        for local_project_group in local_project_groups:
            try:
                reference_group_name = (
                    f"keycloakoidc_group://{local_project_group.name}"
                )
                rancher_backend.get_or_create_project_group_role(
                    reference_group_name,
                    project.backend_id,
                    local_project_group.role.name,
                )
            except exceptions.RancherException as e:
                logger.error(
                    "Unable to sync group %s binding for project %s, reason: %s",
                    local_project_group.name,
                    project.name,
                    e,
                )

    def remove_stale_project_groups(
        rancher_backend: backend.RancherBackend,
        project: models.Project,
        local_project_groups: list[models.KeycloakGroup],
    ):
        remote_project_groups_all = rancher_backend.client.get_project_group_role(
            project_id=project.backend_id
        )
        remote_cluster_groups_filtered = [
            item
            for item in remote_project_groups_all
            if item["groupPrincipalId"] is not None
            and "keycloakoidc_group://" in item["groupPrincipalId"]
        ]
        remote_project_groups = {
            group["groupPrincipalId"]: group for group in remote_cluster_groups_filtered
        }
        local_project_group_principal_names = [
            f"keycloakoidc_group://{group.name}" for group in local_project_groups
        ]
        remote_stale_project_group_ids = (
            remote_project_groups.keys() - local_project_group_principal_names
        )
        for remote_stale_group_id in remote_stale_project_group_ids:
            try:
                remote_group = remote_project_groups[remote_stale_group_id]
                rancher_backend.delete_project_group_role(
                    remote_stale_group_id,
                    project.backend_id,
                    remote_group["roleTemplateId"],
                )
            except exceptions.RancherException as e:
                logger.error(
                    "Unable to delete group %s binding for project %s, reason: %s",
                    remote_stale_group_id,
                    project.name,
                    e,
                )

    clusters = models.Cluster.objects.filter(state=CoreStates.OK)
    for cluster in clusters:
        rancher_backend: backend.RancherBackend = cluster.get_backend()
        local_cluster_groups = list(
            models.KeycloakGroup.objects.filter(
                role__settings=cluster.settings,
                role__scope_type=enums.RoleScopeType.CLUSTER,
            )
        )
        # Create missing cluster groups in Rancher
        create_missing_cluster_groups(rancher_backend, cluster, local_cluster_groups)
        # Delete stale cluster groups in Rancher
        remove_stale_cluster_groups(rancher_backend, cluster, local_cluster_groups)

        projects = models.Project.objects.filter(cluster=cluster)
        for project in projects:
            local_project_groups = list(
                models.KeycloakGroup.objects.filter(
                    role__settings=project.settings,
                    role__scope_type=enums.RoleScopeType.PROJECT,
                )
            )
            # Create missing project groups in Rancher
            create_missing_project_groups(
                rancher_backend, project, local_project_groups
            )
            # Delete stale project groups in Rancher
            remove_stale_project_groups(rancher_backend, project, local_project_groups)
