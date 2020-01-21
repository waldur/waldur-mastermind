from django.core.exceptions import ObjectDoesNotExist
from django.conf import settings
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers
import yaml

from waldur_core.quotas import exceptions as quotas_exceptions
from waldur_openstack.openstack_tenant import models as openstack_tenant_models

from . import models


def get_unique_node_name(name, instance_spl, cluster_spl):
    names_instances = openstack_tenant_models.Instance.objects.filter(service_project_link=instance_spl)\
        .values_list('name', flat=True)
    names_nodes = models.Node.objects.filter(cluster__service_project_link=cluster_spl).values_list('name', flat=True)
    names = list(names_instances) + list(names_nodes)

    if name not in names:
        return name

    i = 1
    new_name = name

    while new_name in names:
        i += 1
        new_name = '%s_%s' % (name, i)

    return new_name


def expand_added_nodes(nodes, rancher_spl, tenant_settings, cluster_name):
    project = rancher_spl.project

    try:
        tenant_spl = openstack_tenant_models.OpenStackTenantServiceProjectLink.objects.get(
            project=project,
            service__settings=tenant_settings)
    except ObjectDoesNotExist:
        raise serializers.ValidationError(
            'Service project link for service %s and project %s is not found.' % (
                tenant_settings.name, project.name
            ))

    try:
        base_image_name = rancher_spl.service.settings.get_option('base_image_name')
        image = openstack_tenant_models.Image.objects.get(
            name=base_image_name,
            settings=tenant_settings)
    except ObjectDoesNotExist:
        raise serializers.ValidationError('No matching image found.')

    try:
        group = openstack_tenant_models.SecurityGroup.objects.get(
            name='default',
            settings=tenant_settings)
    except ObjectDoesNotExist:
        raise serializers.ValidationError('Default security group is not found.')

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
                'Subnet %s should belong to the service settings %s.' % (
                    subnet.name, tenant_settings.name,
                ))

        validate_data_volumes(data_volumes, tenant_settings)
        flavor = validate_flavor(flavor, roles, tenant_settings, cpu, memory)

        node['initial_data'] = {
            'flavor': flavor.uuid.hex,
            'vcpu': flavor.cores,
            'ram': flavor.ram,
            'image': image.uuid.hex,
            'subnet': subnet.uuid.hex,
            'tenant_service_project_link': tenant_spl.id,
            'group': group.uuid.hex,
            'system_volume_size': system_volume_size,
            'system_volume_type': system_volume_type and system_volume_type.uuid.hex,
            'data_volumes': [{
                'size': volume['size'],
                'volume_type': volume.get('volume_type') and volume.get('volume_type').uuid.hex,
            } for volume in data_volumes]
        }

        if 'controlplane' in list(roles):
            node['controlplane_role'] = True
        if 'etcd' in list(roles):
            node['etcd_role'] = True
        if 'worker' in list(roles):
            node['worker_role'] = True

        node['name'] = get_unique_node_name(cluster_name + '-rancher-node', tenant_spl, rancher_spl)

    validate_quotas(nodes, tenant_spl)


def validate_data_volumes(data_volumes, tenant_settings):
    for volume in data_volumes:
        volume_type = volume.get('volume_type')
        if volume_type and volume_type.settings != tenant_settings:
            raise serializers.ValidationError(
                'Volume type %s should belong to the service settings %s.' % (
                    volume_type.name, tenant_settings.name,
                ))

    mount_points = [volume['mount_point'] for volume in data_volumes]
    if len(set(mount_points)) != len(mount_points):
        raise serializers.ValidationError('Each mount point can be specified once at most.')


def validate_flavor(flavor, roles, tenant_settings, cpu=None, memory=None):
    if flavor:
        if cpu or memory:
            raise serializers.ValidationError('Either flavor or cpu and memory should be specified.')
    else:
        if not cpu or not memory:
            raise serializers.ValidationError('Either flavor or cpu and memory should be specified.')

    if not flavor:
        flavor = openstack_tenant_models.Flavor.objects.filter(
            cores__gte=cpu,
            ram__gte=memory,
            settings=tenant_settings). \
            order_by('cores', 'ram').first()

    if not flavor:
        raise serializers.ValidationError('No matching flavor found.')

    if flavor.settings != tenant_settings:
        raise serializers.ValidationError(
            'Flavor %s should belong to the service settings %s.' % (
                flavor.name, tenant_settings.name,
            ))

    requirements = list(filter(lambda x: x[0] in list(roles),
                               settings.WALDUR_RANCHER['ROLE_REQUIREMENT'].items()))
    cpu_requirements = max([t[1]['CPU'] for t in requirements])
    ram_requirements = max([t[1]['RAM'] for t in requirements])
    if flavor.cores < cpu_requirements:
        raise serializers.ValidationError('Flavor %s does not meet requirements. CPU requirement is %s'
                                          % (flavor, cpu_requirements))
    if flavor.ram < ram_requirements:
        raise serializers.ValidationError('Flavor %s does not meet requirements. RAM requirement is %s'
                                          % (flavor, ram_requirements))

    return flavor


def validate_quotas(nodes, tenant_spl):
    quota_sources = [
        tenant_spl,
        tenant_spl.project,
        tenant_spl.customer,
        tenant_spl.service,
        tenant_spl.service.settings
    ]
    for quota_name in ['storage', 'vcpu', 'ram']:
        requested = sum(get_node_quota(quota_name, node) for node in nodes)

        for source in quota_sources:
            try:
                quota = source.quotas.get(name=quota_name)
                if quota.limit != -1 and (quota.usage + requested > quota.limit):
                    raise quotas_exceptions.QuotaValidationError(
                        _('"%(name)s" quota is over limit. Required: %(usage)s, limit: %(limit)s.') % dict(
                            name=quota_name, usage=quota.usage + requested, limit=quota.limit))
            except ObjectDoesNotExist:
                pass


def get_node_quota(quota_name, node):
    conf = node['initial_data']
    if quota_name == 'storage':
        data_volumes = conf.get('data_volumes', [])
        return conf['system_volume_size'] + sum(volume['size'] for volume in data_volumes)
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


def format_node_cloud_config(node):
    node_command = format_node_command(node)
    config_template = node.service_project_link.service.settings.get_option('cloud_init_template')
    user_data = config_template.format(command=node_command)
    data_volumes = node.initial_data.get('data_volumes')

    if data_volumes:
        data_volumes = sorted(data_volumes)
        conf = yaml.parse(user_data)

        # First volume is reserved for system volume, other volumes are data volumes

        conf['mounts'] = [
            [format_disk_id(index + 1), volume['mount_point']]
            for index, volume in enumerate(data_volumes)
        ]

        conf['fs_setup'] = [
            {'device': format_disk_id(index + 1), 'filesystem': 'ext4'}
            for index, volume in enumerate(data_volumes)
        ]
        user_data = yaml.dump(conf)

    return user_data
