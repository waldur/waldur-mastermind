import logging

import yaml
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers

from waldur_core.core.models import User
from waldur_core.quotas import exceptions as quotas_exceptions
from waldur_core.structure.models import ProjectRole, ServiceSettings
from waldur_openstack.openstack_tenant import models as openstack_tenant_models
from waldur_openstack.openstack_tenant.views import InstanceViewSet
from waldur_rancher.backend import RancherBackend

from . import exceptions, models

logger = logging.getLogger(__name__)


def get_unique_node_name(name, tenant_settings, rancher_settings, existing_names=None):
    existing_names = existing_names or []
    # This has a potential risk of race condition when requests to create nodes come exactly at the same time.
    # But we consider this use case highly unrealistic and avoid creation of additional complexity
    # to protect against it
    names_instances = openstack_tenant_models.Instance.objects.filter(
        service_settings=tenant_settings
    ).values_list('name', flat=True)
    names_nodes = models.Node.objects.filter(
        cluster__service_settings=rancher_settings
    ).values_list('name', flat=True)
    names = list(names_instances) + list(names_nodes) + existing_names

    i = 1
    new_name = '%s-%s' % (name, i)

    while new_name in names:
        i += 1
        new_name = '%s-%s' % (name, i)

    return new_name


def expand_added_nodes(
    cluster_name,
    nodes,
    project,
    rancher_settings,
    tenant_settings,
    ssh_public_key,
    security_groups=None,
):

    try:
        base_image_name = rancher_settings.get_option('base_image_name')
        image = openstack_tenant_models.Image.objects.get(
            name=base_image_name, settings=tenant_settings
        )
    except ObjectDoesNotExist:
        raise serializers.ValidationError(_('No matching image found.'))

    if not security_groups:
        try:
            default_security_group = openstack_tenant_models.SecurityGroup.objects.get(
                name='default', settings=tenant_settings
            )
            security_groups = [default_security_group]
        except ObjectDoesNotExist:
            raise serializers.ValidationError(_('Default security group is not found.'))

    for node in nodes:
        memory = node.pop('memory', None)
        cpu = node.pop('cpu', None)
        subnet = node.pop('subnet')
        flavor = node.pop('flavor', None)
        roles = node.pop('roles')
        system_volume_size = node.pop('system_volume_size', None)
        system_volume_type = node.pop('system_volume_type', None)
        data_volumes = node.pop('data_volumes', [])

        if subnet.settings != tenant_settings:
            raise serializers.ValidationError(
                _('Subnet %s should belong to the service settings %s.')
                % (subnet.name, tenant_settings.name,)
            )

        validate_data_volumes(data_volumes, tenant_settings)
        flavor = validate_flavor(flavor, roles, tenant_settings, cpu, memory)

        node['initial_data'] = {
            'flavor': flavor.uuid.hex,
            'vcpu': flavor.cores,
            'ram': flavor.ram,
            'image': image.uuid.hex,
            'subnet': subnet.uuid.hex,
            'service_settings': tenant_settings.uuid.hex,
            'project': project.uuid.hex,
            'security_groups': [group.uuid.hex for group in security_groups],
            'system_volume_size': system_volume_size,
            'system_volume_type': system_volume_type and system_volume_type.uuid.hex,
            'data_volumes': [
                {
                    'size': volume['size'],
                    'volume_type': volume.get('volume_type')
                    and volume.get('volume_type').uuid.hex,
                }
                for volume in data_volumes
            ],
        }

        if 'controlplane' in list(roles):
            node['controlplane_role'] = True
        if 'etcd' in list(roles):
            node['etcd_role'] = True
        if 'worker' in list(roles):
            node['worker_role'] = True

        node['name'] = get_unique_node_name(
            cluster_name + '-rancher-node',
            tenant_settings,
            rancher_settings,
            existing_names=[n['name'] for n in nodes if n.get('name')],
        )

        if ssh_public_key:
            node['initial_data']['ssh_public_key'] = ssh_public_key.uuid.hex

    validate_quotas(nodes, tenant_settings, project)


def validate_data_volumes(data_volumes, tenant_settings):
    for volume in data_volumes:
        volume_type = volume.get('volume_type')
        if volume_type and volume_type.settings != tenant_settings:
            raise serializers.ValidationError(
                _('Volume type %s should belong to the service settings %s.')
                % (volume_type.name, tenant_settings.name,)
            )

    mount_points = [
        volume['mount_point'] for volume in data_volumes if volume.get('mount_point')
    ]
    if len(set(mount_points)) != len(mount_points):
        raise serializers.ValidationError(
            _('Each mount point can be specified once at most.')
        )


def validate_flavor(flavor, roles, tenant_settings, cpu=None, memory=None):
    if flavor:
        if cpu or memory:
            raise serializers.ValidationError(
                _('Either flavor or cpu and memory should be specified.')
            )
    else:
        if not cpu or not memory:
            raise serializers.ValidationError(
                _('Either flavor or cpu and memory should be specified.')
            )

    if not flavor:
        flavor = (
            openstack_tenant_models.Flavor.objects.filter(
                cores__gte=cpu, ram__gte=memory, settings=tenant_settings
            )
            .order_by('cores', 'ram')
            .first()
        )

    if not flavor:
        raise serializers.ValidationError(_('No matching flavor found.'))

    if flavor.settings != tenant_settings:
        raise serializers.ValidationError(
            _('Flavor %s should belong to the service settings %s.')
            % (flavor.name, tenant_settings.name,)
        )

    requirements = list(
        filter(
            lambda x: x[0] in list(roles),
            settings.WALDUR_RANCHER['ROLE_REQUIREMENT'].items(),
        )
    )
    if requirements:
        cpu_requirements = max([t[1]['CPU'] for t in requirements])
        ram_requirements = max([t[1]['RAM'] for t in requirements])
        if flavor.cores < cpu_requirements:
            raise serializers.ValidationError(
                _('Flavor %s does not meet requirements. CPU requirement is %s')
                % (flavor, cpu_requirements)
            )
        if flavor.ram < ram_requirements:
            raise serializers.ValidationError(
                'Flavor %s does not meet requirements. RAM requirement is %s'
                % (flavor, ram_requirements)
            )

    return flavor


def validate_quotas(nodes, tenant_settings, project):
    quota_sources = [
        project,
        project.customer,
        tenant_settings,
    ]
    for quota_name in ['storage', 'vcpu', 'ram']:
        requested = sum(get_node_quota(quota_name, node) for node in nodes)

        for source in quota_sources:
            try:
                quota = source.quotas.get(name=quota_name)
                if quota.limit != -1 and (quota.usage + requested > quota.limit):
                    raise quotas_exceptions.QuotaValidationError(
                        _(
                            '"%(name)s" quota is over limit. Required: %(usage)s, limit: %(limit)s.'
                        )
                        % dict(
                            name=quota_name,
                            usage=quota.usage + requested,
                            limit=quota.limit,
                        )
                    )
            except ObjectDoesNotExist:
                pass


def get_node_quota(quota_name, node):
    conf = node['initial_data']
    if quota_name == 'storage':
        data_volumes = conf.get('data_volumes', [])
        return conf['system_volume_size'] + sum(
            volume['size'] for volume in data_volumes
        )
    else:
        return conf[quota_name]


def format_disk_id(index):
    return '/dev/vd' + (chr(ord('a') + index))


def format_node_command(node):
    roles_command = []

    if node.controlplane_role:
        roles_command.append('--controlplane')

    if node.etcd_role:
        roles_command.append('--etcd')

    if node.worker_role:
        roles_command.append('--worker')

    return node.cluster.node_command + ' ' + ' '.join(roles_command)


def format_node_cloud_config(node: models.Node):
    node_command = format_node_command(node)
    config_template = node.cluster.service_settings.get_option('cloud_init_template')
    user_data = config_template.format(command=node_command)
    data_volumes = node.initial_data.get('data_volumes')

    if data_volumes:
        data_volumes = sorted(data_volumes)
        conf = yaml.safe_load(user_data)

        # First volume is reserved for system volume, other volumes are data volumes

        conf['mounts'] = [
            [format_disk_id(index + 1), volume['mount_point']]
            for index, volume in enumerate(data_volumes)
            if volume.get('mount_point')
        ]

        conf['fs_setup'] = [
            {'device': format_disk_id(index + 1), 'filesystem': 'ext4'}
            for index, volume in enumerate(data_volumes)
        ]
        user_data = yaml.dump(conf)

    return user_data


class SyncUser:
    @staticmethod
    def get_users():
        result = {}

        def add_to_result():
            if user not in result.keys():
                result[user] = {}

            if service_settings not in result[user].keys():
                result[user][service_settings] = []

            result[user][service_settings].append([cluster, role])

        for cluster in models.Cluster.objects.filter(state=models.Cluster.States.OK):
            service_settings = cluster.service_settings
            project = cluster.project
            users = project.get_users()
            owners = project.customer.get_owners()

            for user in users:
                role = (
                    'manager'
                    if project.has_user(user, ProjectRole.MANAGER)
                    else 'admin'
                    if project.has_user(user, ProjectRole.ADMINISTRATOR)
                    else None
                )
                add_to_result()

            for user in owners:
                role = 'owner'
                add_to_result()

        return result

    @staticmethod
    def create_users(add_users):
        count_created = 0
        count_activated = 0
        for user in add_users:
            for service_settings in add_users[user].keys():
                try:
                    with transaction.atomic():
                        (
                            rancher_user,
                            created,
                        ) = models.RancherUser.objects.get_or_create(
                            settings=service_settings, user=user,
                        )
                        backend = RancherBackend(service_settings)

                        if created:
                            backend.create_user(rancher_user)
                            count_created += 1
                        else:
                            backend.activate_user(rancher_user)
                            count_activated += 1
                except exceptions.RancherException as e:
                    logger.error('Error creating or activating user %s. %s' % (user, e))

        return count_created, count_activated

    @staticmethod
    def block_users(rancher_users):
        count = 0

        for user in rancher_users:
            try:
                with transaction.atomic():
                    backend = RancherBackend(user.settings)
                    backend.block_user(user)
                    count += 1
            except exceptions.RancherException as e:
                logger.error('Error blocking user %s. %s' % (user, e))
        return count

    @staticmethod
    def update_users_roles(users):
        count = 0

        for user in users:
            for service_settings in users[user]:
                has_change = False
                rancher_user = models.RancherUser.objects.get(
                    user=user, settings=service_settings
                )
                current_links = models.RancherUserClusterLink.objects.filter(
                    user=rancher_user
                )
                actual_links = users[user][service_settings]
                current_links_set = {
                    (link.cluster.id, link.role) for link in current_links
                }

                actual_links_set = set()

                for link in actual_links:
                    role = (
                        models.ClusterRole.CLUSTER_OWNER
                        if link[1] in ['owner', 'manager']
                        else models.ClusterRole.CLUSTER_MEMBER
                        if link[1] in ['admin']
                        else None
                    )
                    actual_links_set.add((link[0].id, role))

                remove_links = current_links_set - actual_links_set
                add_links = actual_links_set - current_links_set

                backend = RancherBackend(service_settings)

                for link in remove_links:
                    cluster_id = link[0]
                    role = link[1]
                    rancher_user_cluster_link = models.RancherUserClusterLink.objects.get(
                        user=rancher_user, role=role, cluster_id=cluster_id
                    )

                    try:
                        with transaction.atomic():
                            backend.delete_cluster_role(rancher_user_cluster_link)

                        has_change = True
                    except exceptions.RancherException as e:
                        logger.error(
                            'Error deleting role %s. %s'
                            % (rancher_user_cluster_link.id, e)
                        )

                for link in add_links:
                    cluster_id = link[0]
                    role = link[1]

                    try:
                        with transaction.atomic():
                            rancher_user_cluster_link = models.RancherUserClusterLink.objects.create(
                                user=rancher_user, role=role, cluster_id=cluster_id
                            )
                            backend.create_cluster_role(rancher_user_cluster_link)

                        has_change = True
                    except exceptions.RancherException as e:
                        logger.error(
                            'Error creating role. User ID: %s, cluster ID: %s, role: %s. %s'
                            % (rancher_user.id, cluster_id, role, e)
                        )

                if has_change:
                    count += 1

        return count

    @staticmethod
    def update_users_project_roles(users):
        count_deleted = 0
        count_created = 0
        roles_cache = {}

        def get_roles(service_settings):
            if service_settings not in roles_cache.keys():
                backend = RancherBackend(service_settings)
                roles_cache[service_settings] = backend.client.get_projects_roles()

            return roles_cache[service_settings]

        for user in users:
            for service_settings in users[user]:
                rancher_user = models.RancherUser.objects.get(
                    user=user, settings=service_settings
                )

                if not rancher_user.backend_id:
                    continue

                roles = get_roles(service_settings)
                roles_ids = [role['id'] for role in roles]
                deleted, _ = (
                    models.RancherUserProjectLink.objects.filter(user=rancher_user)
                    .exclude(backend_id__in=roles_ids)
                    .delete()
                )
                count_deleted += deleted
                actual_roles = [
                    {
                        'project_id': role['projectId'],
                        'id': role['id'],
                        'role_template_id': role['roleTemplateId'],
                    }
                    for role in roles
                    if role['userId'] == rancher_user.backend_id
                ]

                for role in actual_roles:
                    try:
                        project = models.Project.objects.get(
                            backend_id=role['project_id']
                        )
                        (
                            _,
                            created,
                        ) = models.RancherUserProjectLink.objects.update_or_create(
                            backend_id=role['id'],
                            user=rancher_user,
                            project=project,
                            defaults={'role': role['role_template_id']},
                        )
                        count_created += created
                    except models.Project.DoesNotExist:
                        logger.warning(
                            'Project with backend ID %s is not found.'
                            % role['project_id']
                        )

        return count_deleted, count_created

    @classmethod
    def run(cls):
        if settings.WALDUR_RANCHER['DISABLE_AUTOMANAGEMENT_OF_USERS']:
            return {}
        result = {}
        actual_users = cls.get_users()
        current_users = models.RancherUser.objects.filter(is_active=True)
        current_users_set = {
            (user.user.uuid.hex, user.settings.uuid.hex) for user in current_users
        }
        actual_users_set = set()

        for user in actual_users:
            for service_settings in actual_users[user]:
                actual_users_set.add((user.uuid.hex, service_settings.uuid.hex))

        # Delete users
        remove_rancher_users_set = current_users_set - actual_users_set
        remove_rancher_users = []

        for user in remove_rancher_users_set:
            user_uuid = user[0]
            settings_uuid = user[1]
            remove_rancher_users.append(
                models.RancherUser.objects.get(
                    user__uuid=user_uuid, settings__uuid=settings_uuid
                )
            )

        result['blocked'] = cls.block_users(remove_rancher_users)

        # Create users
        add_users_set = actual_users_set - current_users_set
        add_users = {}

        for user in add_users_set:
            user_uuid = user[0]
            settings_uuid = user[1]
            user = User.objects.get(uuid=user_uuid)
            service_settings = ServiceSettings.objects.get(uuid=settings_uuid)

            if user not in add_users.keys():
                add_users[user] = {}

            if service_settings not in add_users[user].keys():
                add_users[user][service_settings] = []

            add_users[user][service_settings].extend(
                actual_users[user][service_settings]
            )

        result['created'], result['activated'] = cls.create_users(add_users)

        # Update user's roles
        result['updated'] = cls.update_users_roles(actual_users)

        # Update user's project roles
        (
            result['project roles deleted'],
            result['project roles created'],
        ) = cls.update_users_project_roles(actual_users)

        return result


def update_cluster_nodes_states(cluster_id):
    cluster = models.Cluster.objects.get(id=cluster_id)

    for node in cluster.node_set.exclude(backend_id=''):
        old_state = node.state

        if node.runtime_state == models.Node.RuntimeStates.ACTIVE:
            node.state = models.Node.States.OK
        elif (
            node.runtime_state
            in [
                models.Node.RuntimeStates.REGISTERING,
                models.Node.RuntimeStates.UNAVAILABLE,
            ]
            or not node.runtime_state
        ):
            node.state = models.Node.States.CREATING
        elif node.runtime_state:
            node.state = models.Node.States.ERRED

        if old_state != node.state:
            node.save(update_fields=['state'])


def _check_permissions(action):
    def func(request, view, instance=None):
        node = instance

        if not node:
            return

        validators = getattr(InstanceViewSet, action + '_permissions')

        for validator in validators:
            if node.instance:
                validator(request, view, node.instance)

    return func


def check_permissions_for_console():
    return _check_permissions('console')


def check_permissions_for_console_log():
    return _check_permissions('console_log')
