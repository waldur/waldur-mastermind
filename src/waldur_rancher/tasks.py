import logging

from celery import shared_task
from django.conf import settings
from django.contrib import auth
from django.contrib.contenttypes.models import ContentType
from rest_framework import status
from rest_framework.reverse import reverse

from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils as core_utils
from waldur_core.core.exceptions import RuntimeStateException
from waldur_core.structure.signals import resource_imported
from waldur_mastermind.common import utils as common_utils
from waldur_openstack import models as openstack_models
from waldur_openstack.views import MarketplaceInstanceViewSet
from waldur_rancher.enums import LONGHORN_NAME, LONGHORN_NAMESPACE
from waldur_rancher.utils import SyncUser

from . import backend, exceptions, models, utils

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

    def execute(self, instance, user_id):
        node = instance
        content_type = ContentType.objects.get_for_model(openstack_models.Instance)
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
        user = auth.get_user_model().objects.get(pk=user_id)
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
                    "vault_secret_path": f"rancher/cluster-{node.cluster.uuid.hex}",
                    "vault_role_id": role_id,
                    "vault_role_secret_id": role_secret_id,
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

        instance_uuid = response.data["uuid"]
        instance = openstack_models.Instance.objects.get(uuid=instance_uuid)
        node.content_type = content_type
        node.object_id = instance.id
        node.state = models.Node.States.CREATING
        node.save()

        resource_imported.send(
            sender=instance.__class__,
            instance=instance,
        )

    @classmethod
    def get_description(cls, instance, *args, **kwargs):
        return 'Create nodes for k8s cluster "%s".' % instance


class DeleteNodeTask(core_tasks.Task):
    def execute(self, instance, user_id):
        node = instance
        user = auth.get_user_model().objects.get(pk=user_id)

        if node.instance:
            view = MarketplaceInstanceViewSet.as_view({"delete": "force_destroy"})
            response = common_utils.delete_request(
                view,
                user,
                uuid=node.instance.uuid.hex,
                query_params={"delete_volumes": True},
            )

            if response.status_code != status.HTTP_202_ACCEPTED:
                raise exceptions.RancherException(response.data)
        else:
            backend = node.cluster.get_backend()
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
    for cluster in models.Cluster.objects.exclude(backend_id=""):
        pull_cluster_nodes(cluster.id)
        utils.update_cluster_nodes_states(cluster.id)


class PollRuntimeStateNodeTask(core_tasks.Task):
    max_retries = 600
    default_retry_delay = 10

    @classmethod
    def get_description(cls, node, *args, **kwargs):
        node = core_utils.deserialize_instance(node)
        return 'Poll node "%s"' % node.name

    def execute(self, node):
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


@shared_task(name="waldur_rancher.notify_create_user")
def notify_create_user(id, password, url):
    user = models.RancherUser.objects.get(id=id).user

    if not user.email or not user.notifications_enabled:
        return

    context = {
        "rancher_url": url,
        "user": user,
        "password": password,
    }

    core_utils.broadcast_mail(
        "rancher", "notification_create_user", context, [user.email]
    )


@shared_task(name="waldur_rancher.sync_users")
def sync_users():
    if settings.WALDUR_RANCHER["READ_ONLY_MODE"]:
        return
    SyncUser.run()


class PollLonghornApplicationTask(core_tasks.Task):
    max_retries = 600
    default_retry_delay = 10

    @classmethod
    def get_description(cls, cluster, *args, **kwargs):
        cluster = core_utils.deserialize_instance(cluster)
        return 'Poll Longhorn application runtime state for cluster "%s"' % cluster.name

    def execute(self, cluster):
        app = models.Application.objects.get(
            cluster=cluster, name=LONGHORN_NAME, namespace__name=LONGHORN_NAMESPACE
        )
        backend = app.get_backend()
        backend.check_application_state(app)
        if app.runtime_state == "active":
            app.state = models.Application.States.OK
            app.save()
        elif app.runtime_state == "error":
            app.state = models.Application.States.ERRED
            app.save()

        if app.runtime_state not in ("active", "error"):
            self.retry()
        elif app.runtime_state == "error":
            raise RuntimeStateException(
                f"{app.__class__.__name__} (PK: {app.pk}) runtime state become erred: {app.runtime_state}"
            )

        return app


class CreateVaultCredentialsTask(core_tasks.Task):
    @classmethod
    def get_description(cls, cluster, *args, **kwargs):
        cluster = core_utils.deserialize_instance(cluster)
        return "Create secret and temporary secret id for it in Vault for cluster %s"

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

        all_tokens = [{"clusterId": "test-00", "token": "secretdata"}]
        cluster_tokens = [
            token for token in all_tokens if token["clusterId"] == cluster.backend_id
        ]
        token = cluster_tokens[0]["token"]

        vault_backend.create_or_update_secret(secret_name, {"token": token})
