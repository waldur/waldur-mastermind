from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure.models import VirtualMachine
from waldur_core.quotas import exceptions as quotas_exceptions
from waldur_openstack.openstack_tenant import models as openstack_tenant_models

from . import models, validators, exceptions


class ServiceSerializer(core_serializers.ExtraFieldOptionsMixin,
                        structure_serializers.BaseServiceSerializer):

    SERVICE_ACCOUNT_FIELDS = {
        'backend_url': _('Rancher server URL'),
        'username': _('Rancher access key'),
        'password': _('Rancher secret key'),
    }

    SERVICE_ACCOUNT_EXTRA_FIELDS = {
        'base_image_name': _('Base image name'),
    }

    class Meta(structure_serializers.BaseServiceSerializer.Meta):
        model = models.RancherService
        required_fields = ('backend_url', 'username', 'password', 'base_image_name')


class ServiceProjectLinkSerializer(structure_serializers.BaseServiceProjectLinkSerializer):
    class Meta(structure_serializers.BaseServiceProjectLinkSerializer.Meta):
        model = models.RancherServiceProjectLink
        extra_kwargs = {
            'service': {'lookup_field': 'uuid', 'view_name': 'vmware-detail'},
        }


class NestedNodeSerializer(serializers.HyperlinkedModelSerializer):
    instance = core_serializers.GenericRelatedField(
        related_models=VirtualMachine.get_all_models(),
        read_only=True
    )

    subnet = serializers.HyperlinkedRelatedField(
        view_name='openstacktenant-subnet-detail',
        queryset=openstack_tenant_models.SubNet.objects.all(),
        lookup_field='uuid',
        allow_null=True,
        write_only=True,
    )
    storage = serializers.IntegerField(write_only=True)
    memory = serializers.IntegerField(write_only=True)
    cpu = serializers.IntegerField(write_only=True)
    roles = serializers.MultipleChoiceField(choices=['controlplane', 'etcd', 'worker'], write_only=True)

    class Meta(object):
        model = models.Node
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'rancher-node-detail'},
            'cluster': {'lookup_field': 'uuid', 'view_name': 'rancher-cluster-detail'}
        }
        exclude = ('cluster', 'object_id', 'content_type', 'name')


class ClusterSerializer(structure_serializers.BaseResourceSerializer):
    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name='rancher-detail',
        read_only=True,
        lookup_field='uuid',
    )

    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='rancher-spl-detail',
        queryset=models.RancherServiceProjectLink.objects.all(),
        allow_null=True,
        required=False,
    )
    name = serializers.CharField(max_length=150, validators=[validators.ClusterNameValidator])
    nodes = NestedNodeSerializer(many=True, source='node_set')

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Cluster
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'node_command', 'nodes',
        )
        read_only_fields = structure_serializers.BaseResourceSerializer.Meta.read_only_fields + (
            'node_command',
        )
        protected_fields = structure_serializers.BaseResourceSerializer.Meta.protected_fields + ('nodes',)
        extra_kwargs = dict(
            cluster={
                'view_name': 'rancher-cluster-detail',
                'lookup_field': 'uuid',
            },
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs
        )

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        nodes = attrs.get('node_set')
        spl = attrs.get('service_project_link')

        for node in nodes:
            error_message = {}

            for node_param in ['storage', 'memory', 'cpu', 'subnet']:

                if not node.get(node_param):
                    error_message[node_param] = 'This field is required.'

            if error_message:
                raise serializers.ValidationError(error_message)

            memory = node.pop('memory')
            cpu = node.pop('cpu')
            subnet = node.pop('subnet')
            roles = node.pop('roles')

            try:
                settings = subnet.settings
                project = spl.project
                instance_spl = openstack_tenant_models.OpenStackTenantServiceProjectLink.objects.get(
                    project=project,
                    service__settings=settings)
            except ObjectDoesNotExist:
                raise serializers.ValidationError('No matching instance service project link found.')

            flavors = openstack_tenant_models.Flavor.objects.filter(
                cores__gte=cpu,
                ram__gte=memory,
                settings=instance_spl.service.settings).\
                order_by('cores', 'ram')

            if not flavors:
                raise serializers.ValidationError('No matching flavor found.')

            try:
                base_image_name = spl.service.settings.get_option('base_image_name')
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

            flavor = flavors[0]
            node['flavor'] = flavor.uuid
            node['vcpu'] = flavor.cores
            node['ram'] = flavor.ram
            node['image'] = image.uuid
            node['subnet'] = subnet.uuid
            node['tenant_service_project_link'] = instance_spl.id
            node['roles'] = list(roles)
            node['group'] = group.uuid

        # check quotas
        quota_sources = [
            instance_spl,
            instance_spl.project,
            instance_spl.customer,
            instance_spl.service,
            instance_spl.service.settings
        ]

        for quota_name in ['storage', 'vcpu', 'ram']:
            requested = sum([node[quota_name] for node in nodes])

            for source in quota_sources:
                try:
                    quota = source.quotas.get(name=quota_name)
                    if quota.limit != -1 and (quota.usage + requested > quota.limit):
                        raise quotas_exceptions.QuotaValidationError(
                            _('"%(name)s" quota is over limit. Required: %(usage)s, limit: %(limit)s.') % dict(
                                name=quota_name, usage=quota.usage + requested, limit=quota.limit))
                except ObjectDoesNotExist:
                    pass

        return super(ClusterSerializer, self).validate(attrs)

    def validate_nodes(self, nodes):
        if len([node for node in nodes if 'etcd' in node['roles']]) not in [1, 3, 5]:
            raise serializers.ValidationError(
                'Total count of etcd nodes must be 1, 3 or 5. You have got %s nodes.' % len(nodes)
            )

        if not len([node for node in nodes if 'worker' in node['roles']]):
            raise serializers.ValidationError('Count of workers roles must be min 1.')

        if not len([node for node in nodes if 'controlplane' in node['roles']]):
            raise serializers.ValidationError('Count of controlplane nodes must be min 1.')

        return nodes


class NodeSerializer(serializers.HyperlinkedModelSerializer):
    instance = core_serializers.GenericRelatedField(
        related_models=VirtualMachine.get_all_models(),
        required=True,
    )

    class Meta:
        model = models.Node
        fields = ('uuid', 'url', 'created', 'modified', 'cluster', 'instance', 'controlplane_role', 'etcd_role',
                  'worker_role', 'get_node_command')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'rancher-node-detail'},
            'cluster': {'lookup_field': 'uuid', 'view_name': 'rancher-cluster-detail'}
        }

    def validate(self, attrs):
        instance = attrs.get('instance')

        if models.Node.objects.filter(
                object_id=instance.id,
                content_type=ContentType.objects.get_for_model(instance)
        ).exists():
            raise serializers.ValidationError({'instance': 'The selected instance is already in use.'})

        attrs['name'] = instance.name

        return super(NodeSerializer, self).validate(attrs)


class ClusterImportableSerializer(serializers.Serializer):
    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='rancher-spl-detail',
        queryset=models.RancherServiceProjectLink.objects.all(),
        write_only=True)

    name = serializers.CharField(read_only=True)
    backend_id = serializers.CharField(source="id", read_only=True)
    kubernetes_version = serializers.CharField(
        source="rancherKubernetesEngineConfig.kubernetesVersion",
        read_only=True)
    created_ts = serializers.IntegerField(read_only=True)
    nodes = serializers.ListField(
        source="appliedSpec.rancherKubernetesEngineConfig.nodes",
        required=False,
        read_only=True)


class ClusterImportSerializer(ClusterImportableSerializer):
    backend_id = serializers.CharField(write_only=True)

    @transaction.atomic
    def create(self, validated_data):
        service_project_link = validated_data['service_project_link']
        backend_id = validated_data['backend_id']

        if models.Cluster.objects.filter(
            service_project_link__service__settings=service_project_link.service.settings,
            backend_id=backend_id
        ).exists():
            raise serializers.ValidationError({'backend_id': _('Cluster has been imported already.')})

        try:
            backend = service_project_link.get_backend()
            cluster = backend.import_cluster(backend_id, service_project_link=service_project_link)
        except exceptions.RancherException:
            raise serializers.ValidationError({
                'backend_id': _("Can't import cluster with ID %s") % validated_data['backend_id']
            })

        return cluster


class LinkOpenstackSerializer(serializers.Serializer):
    instance = serializers.HyperlinkedRelatedField(
        view_name='openstacktenant-instance-detail',
        queryset=openstack_tenant_models.Instance.objects.all(),
        lookup_field='uuid',
        write_only=True)
