from django.core.exceptions import ObjectDoesNotExist
from django.conf import settings as conf_settings
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers

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


def expand_added_nodes(nodes, cluster_spl, cluster_name):
    for node in nodes:
        error_message = {}

        for node_param in ['storage', 'subnet']:

            if not node.get(node_param):
                error_message[node_param] = 'This field is required.'

        if not node.get('flavor') and not (node.get('cpu') and node.get('memory')):
            error_message = 'You must specify flavor or cpu and memory.'

        if error_message:
            raise serializers.ValidationError(error_message)

        memory = node.pop('memory', None)
        cpu = node.pop('cpu', None)
        subnet = node.pop('subnet')
        flavor = node.pop('flavor', None)
        roles = node.pop('roles')
        storage = node.pop('storage')

        if flavor:
            if flavor.settings != subnet.settings:
                raise serializers.ValidationError('Subnet and flavor settings are not equal.')

        try:
            settings = subnet.settings
            project = cluster_spl.project
            instance_spl = openstack_tenant_models.OpenStackTenantServiceProjectLink.objects.get(
                project=project,
                service__settings=settings)
        except ObjectDoesNotExist:
            raise serializers.ValidationError('No matching instance service project link found.')

        if not flavor:
            flavors = openstack_tenant_models.Flavor.objects.filter(
                cores__gte=cpu,
                ram__gte=memory,
                settings=instance_spl.service.settings). \
                order_by('cores', 'ram')
            flavor = flavors[0]

            if not flavors:
                raise serializers.ValidationError('No matching flavor found.')

        # validate flavor
        requirements = list(filter(lambda x: x[0] in list(roles),
                                   conf_settings.WALDUR_RANCHER['ROLE_REQUIREMENT'].items()))
        cpu_requirements = max([t[1]['CPU'] for t in requirements])
        ram_requirements = max([t[1]['RAM'] for t in requirements])

        if flavor.cores < cpu_requirements:
            raise serializers.ValidationError('Flavor %s does not meet requirements. CPU requirement is %s'
                                              % (flavor, cpu_requirements))

        if flavor.ram < ram_requirements:
            raise serializers.ValidationError('Flavor %s does not meet requirements. RAM requirement is %s'
                                              % (flavor, ram_requirements))

        try:
            base_image_name = cluster_spl.service.settings.get_option('base_image_name')
            image = openstack_tenant_models.Image.objects.get(
                name=base_image_name,
                settings=instance_spl.service.settings)
        except ObjectDoesNotExist:
            raise serializers.ValidationError('No matching image found.')

        try:
            group = openstack_tenant_models.SecurityGroup.objects.get(
                name='default',
                settings=instance_spl.service.settings)
        except ObjectDoesNotExist:
            raise serializers.ValidationError('No matching group found.')

        node['initial_data'] = {
            'flavor': flavor.uuid.hex,
            'vcpu': flavor.cores,
            'ram': flavor.ram,
            'image': image.uuid.hex,
            'subnet': subnet.uuid.hex,
            'tenant_service_project_link': instance_spl.id,
            'group': group.uuid.hex,
            'storage': storage,
        }

        if 'controlplane' in list(roles):
            node['controlplane_role'] = True
        if 'etcd' in list(roles):
            node['etcd_role'] = True
        if 'worker' in list(roles):
            node['worker_role'] = True

        name = cluster_name + '_rancher_node'
        unique_name = get_unique_node_name(name, instance_spl.id, cluster_spl)
        node['name'] = unique_name

        # check quotas
        quota_sources = [
            instance_spl,
            instance_spl.project,
            instance_spl.customer,
            instance_spl.service,
            instance_spl.service.settings
        ]

        for quota_name in ['storage', 'vcpu', 'ram']:
            requested = sum([node['initial_data'][quota_name] for node in nodes])

            for source in quota_sources:
                try:
                    quota = source.quotas.get(name=quota_name)
                    if quota.limit != -1 and (quota.usage + requested > quota.limit):
                        raise quotas_exceptions.QuotaValidationError(
                            _('"%(name)s" quota is over limit. Required: %(usage)s, limit: %(limit)s.') % dict(
                                name=quota_name, usage=quota.usage + requested, limit=quota.limit))
                except ObjectDoesNotExist:
                    pass
