import logging
import textwrap
from typing import cast

import yaml
from constance import config
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.utils.translation import gettext as _
from rest_framework import serializers

from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.core.models import SshPublicKey
from waldur_core.quotas import exceptions as quotas_exceptions
from waldur_core.quotas.models import QuotaModelMixin
from waldur_core.structure.models import Project, ServiceSettings
from waldur_openstack import models as openstack_models
from waldur_openstack.models import Flavor, Image, SecurityGroup, SubNet, Tenant
from waldur_openstack.utils import (
    is_flavor_valid_for_tenant,
    is_volume_type_valid_for_tenant,
)
from waldur_openstack.views import InstanceViewSet
from waldur_rancher.enums import (
    KeycloakUserGroupMembershipState,
    NodeRoleType,
    RoleScopeType,
)

from . import models

logger = logging.getLogger(__name__)


def get_unique_node_name(
    name, tenant: openstack_models.Tenant, rancher_settings, role, existing_names=None
):
    existing_names = existing_names or []
    # This has a potential risk of race condition when requests to create nodes come exactly at the same time.
    # But we consider this use case highly unrealistic and avoid creation of additional complexity
    # to protect against it
    names_instances = openstack_models.Instance.objects.filter(
        tenant=tenant
    ).values_list("name", flat=True)
    names_nodes = models.Node.objects.filter(
        cluster__service_settings=rancher_settings
    ).values_list("name", flat=True)
    names = list(names_instances) + list(names_nodes) + existing_names

    i = 1
    new_name = f"{name}-{role}-{i}"

    while new_name in names:
        i += 1
        new_name = f"{name}-{role}-{i}"

    return new_name


def expand_added_nodes(
    cluster_name: str,
    nodes: list[dict],
    project: Project,
    rancher_settings: ServiceSettings,
    tenant: Tenant | None,
    ssh_public_key: SshPublicKey | None,
    security_groups=None,
):
    for node in nodes:
        node_tenant = node.pop("tenant", None)
        if not tenant:
            tenant = node_tenant
        if not tenant:
            raise serializers.ValidationError(
                "Tenant is not specified for both cluster and node."
            )
        valid_images = Image.objects.filter(tenants=tenant)
        try:
            base_image_name = rancher_settings.get_option("base_image_name")
            image = valid_images.get(name=base_image_name)
        except ObjectDoesNotExist:
            raise serializers.ValidationError(_("No matching image found."))

        if not security_groups:
            try:
                default_security_group = SecurityGroup.objects.get(
                    name="default", tenant=tenant
                )
                security_groups = [default_security_group]
            except ObjectDoesNotExist:
                raise serializers.ValidationError(
                    _("Default security group is not found.")
                )
        subnet = cast(SubNet, node.pop("subnet"))
        flavor = cast(Flavor, node.pop("flavor"))
        role = cast(NodeRoleType, node.pop("role"))
        system_volume_size = node.pop("system_volume_size", None)
        system_volume_type = node.pop("system_volume_type", None)
        data_volumes = node.pop("data_volumes", [])

        if subnet.tenant != tenant:
            raise serializers.ValidationError(
                _("Subnet %s should belong to the same tenant %s.")
                % (subnet.name, tenant.name)
            )

        validate_data_volumes(data_volumes, tenant)
        flavor = validate_flavor(flavor, role, tenant)

        node["initial_data"] = {
            "flavor": flavor.uuid.hex,
            "vcpu": flavor.cores,
            "ram": flavor.ram,
            "image": image.uuid.hex,
            "subnet": subnet.uuid.hex,
            "tenant": tenant.uuid.hex,
            "service_settings": tenant.service_settings.uuid.hex,
            "project": project.uuid.hex,
            "security_groups": [group.uuid.hex for group in security_groups],
            "system_volume_size": system_volume_size,
            "system_volume_type": system_volume_type and system_volume_type.uuid.hex,
            "data_volumes": [
                {
                    "size": volume["size"],
                    "volume_type": volume.get("volume_type")
                    and volume.get("volume_type").uuid.hex,
                    "mount_point": volume.get("mount_point"),
                    "filesystem": volume.get("filesystem"),
                }
                for volume in data_volumes
            ],
        }

        node["name"] = get_unique_node_name(
            cluster_name + "-rancher-node",
            tenant,
            rancher_settings,
            role,
            existing_names=[n["name"] for n in nodes if n.get("name")],
        )

        node["role"] = role

        if ssh_public_key:
            node["initial_data"]["ssh_public_key"] = ssh_public_key.uuid.hex

    if tenant:
        validate_quotas(nodes, tenant, project)
    else:
        for node in nodes:
            validate_quotas(nodes, node["tenant"], project)


def validate_data_volumes(data_volumes, tenant):
    for volume in data_volumes:
        volume_type = volume.get("volume_type")
        if volume_type and not is_volume_type_valid_for_tenant(volume_type, tenant):
            raise serializers.ValidationError(
                _("Volume type %s is not visible in tenant %s.")
                % (volume_type.name, tenant.name)
            )

    mount_points = [
        volume["mount_point"] for volume in data_volumes if volume.get("mount_point")
    ]
    if len(set(mount_points)) != len(mount_points):
        raise serializers.ValidationError(
            _("Each mount point can be specified once at most.")
        )


def validate_flavor(
    flavor: Flavor,
    role: NodeRoleType,
    tenant: Tenant,
):
    if not is_flavor_valid_for_tenant(flavor, tenant):
        raise serializers.ValidationError(
            _("Flavor %s is not visible in tenant %s.") % (flavor.name, tenant)
        )

    requirements = settings.WALDUR_RANCHER["ROLE_REQUIREMENT"].get(role)
    if requirements:
        cpu_requirements = requirements["CPU"]
        ram_requirements = requirements["RAM"]
        if flavor.cores < cpu_requirements:
            raise serializers.ValidationError(
                _("Flavor %s does not meet requirements. CPU requirement is %s")
                % (flavor, cpu_requirements)
            )
        if flavor.ram < ram_requirements:
            raise serializers.ValidationError(
                f"Flavor {flavor} does not meet requirements. RAM requirement is {ram_requirements}"
            )

    return flavor


def validate_quotas(nodes, tenant: Tenant, project: Project):
    quota_sources: list[QuotaModelMixin] = [
        project,
        project.customer,
        tenant,
    ]
    for quota_name in ["storage", "vcpu", "ram"]:
        requested = sum(get_node_quota(quota_name, node) for node in nodes)

        for source in quota_sources:
            try:
                limit = source.get_quota_limit(quota_name)
                usage = source.get_quota_usage(quota_name)
                if limit != -1 and (usage + requested > limit):
                    raise quotas_exceptions.QuotaValidationError(
                        _(
                            '"%(name)s" quota is over limit. Required: %(usage)s, limit: %(limit)s.'
                        )
                        % dict(
                            name=quota_name,
                            usage=usage + requested,
                            limit=limit,
                        )
                    )
            except ObjectDoesNotExist:
                pass


def get_node_quota(quota_name, node):
    conf = node["initial_data"]
    if quota_name == "storage":
        data_volumes = conf.get("data_volumes", [])
        return conf["system_volume_size"] + sum(
            volume["size"] for volume in data_volumes
        )
    else:
        return conf[quota_name]


def format_disk_id(disk_driver, index):
    return f"/dev/{disk_driver}{(chr(ord('a') + index))}"


def format_node_cloud_config(
    node: models.Node,
    cloud_init_extra_params: dict | None = None,
):
    cloud_init_extra_params = cloud_init_extra_params or {}
    config_template = cast(
        str | None, node.cluster.service_settings.get_option("cloud_init_template")
    )
    disk_driver = cast(
        str | None, node.cluster.service_settings.get_option("node_disk_driver")
    )
    if not config_template:
        return ""
    user_data = config_template.format(
        **cloud_init_extra_params,
    )
    data_volumes = node.initial_data.get("data_volumes")

    if data_volumes:
        conf = yaml.safe_load(user_data)

        # First volume is reserved for system volume, other volumes are data volumes
        conf["bootcmd"] = [
            textwrap.dedent(f"""\
            filename_to_wait_for="{format_disk_id(disk_driver, index + 1)}"

            # Timeout in seconds
            timeout=600 # 10 minutes

            # Check every `interval` seconds
            interval=5

            elapsed=0
            while [ ! -e "$filename_to_wait_for" ]; do
                sleep "$interval"
                elapsed=$((elapsed + interval))

                if [ "$elapsed" -ge "$timeout" ]; then
                    echo "Timeout reached. File not found: $filename_to_wait_for"
                    exit 1
                fi
            done

            echo "File found: $filename_to_wait_for"
            """)
            for index, _ in enumerate(data_volumes)
        ]

        conf["disk_setup"] = {
            format_disk_id(disk_driver, index + 1): {
                "table_type": "gpt",
                "layout": "true",
                "overwrite": "false",
            }
            for index, _ in enumerate(data_volumes)
        }

        conf["mounts"] = [
            [format_disk_id(disk_driver, index + 1), volume["mount_point"]]
            for index, volume in enumerate(data_volumes)
            if volume.get("mount_point")
        ]

        conf["fs_setup"] = [
            {
                "device": format_disk_id(disk_driver, index + 1),
                "filesystem": volume.get("filesystem", "ext4"),
            }
            for index, volume in enumerate(data_volumes)
        ]
        user_data_raw = yaml.dump(conf, default_style="|")
        user_data = f"#cloud-config\n{user_data_raw}"

    return user_data


def update_cluster_nodes_states(cluster_id):
    cluster = models.Cluster.objects.get(id=cluster_id)

    for node in cluster.node_set.exclude(backend_id=""):
        old_state = node.state

        if node.runtime_state == models.Node.RuntimeStates.ACTIVE:
            node.state = CoreStates.OK
        elif (
            node.runtime_state
            in [
                models.Node.RuntimeStates.REGISTERING,
                models.Node.RuntimeStates.UNAVAILABLE,
            ]
            or not node.runtime_state
        ):
            node.state = CoreStates.CREATING
        elif node.runtime_state:
            node.state = CoreStates.ERRED

        if old_state != node.state:
            node.save(update_fields=["state"])


def _check_permissions(action):
    def func(request, view, instance=None):
        node = instance

        if not node:
            return

        validators = getattr(InstanceViewSet, action + "_permissions")

        for validator in validators:
            if node.instance:
                validator(request, view, node.instance)

    return func


def check_permissions_for_console():
    return _check_permissions("console")


def check_permissions_for_console_log():
    return _check_permissions("console_log")


def get_management_tenant(cluster):
    from waldur_openstack.models import Tenant

    tenant = None

    try:
        tenant_uuid = cluster.settings.get_option("management_tenant_uuid")
        tenant = Tenant.objects.get(uuid=tenant_uuid)
    except ObjectDoesNotExist:
        pass

    return tenant


def send_user_membership_notification_email(
    user: models.KeycloakUserGroupMembership, scope, rancher_url, sync_frequency_minutes
):
    context = {
        "rancher_url": rancher_url,
        "support_email": config.SITE_EMAIL,
        "scope_type": user.group.role.scope_type.capitalize(),  # 'cluster' or 'project'
        "scope_name": scope.name,
        "role": user.group.role,
        "user_exists": user.state == KeycloakUserGroupMembershipState.ACTIVE,
        "sync_frequency_minutes": sync_frequency_minutes,
    }

    core_utils.broadcast_mail(
        "rancher", "rancher_keycloak_membership_notification", context, [user.email]
    )


def get_keycloak_group_scope_and_settings(group: models.KeycloakGroup):
    scope_type = group.role.scope_type
    scope_uuid = group.scope_uuid
    if scope_type == RoleScopeType.CLUSTER:
        scope = models.Cluster.objects.get(uuid=scope_uuid)
        return scope, scope.settings
    else:
        scope = models.Project.objects.get(uuid=scope_uuid)
        return scope, scope.cluster and scope.cluster.settings
