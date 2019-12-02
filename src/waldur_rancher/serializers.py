from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure.models import VirtualMachine
from waldur_openstack.openstack_tenant import models as openstack_tenant_models

from . import models, validators, exceptions, utils


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


class BaseNodeSerializer(serializers.HyperlinkedModelSerializer):
    subnet = serializers.HyperlinkedRelatedField(
        view_name='openstacktenant-subnet-detail',
        queryset=openstack_tenant_models.SubNet.objects.all(),
        lookup_field='uuid',
        allow_null=True,
        write_only=True,
    )
    flavor = serializers.HyperlinkedRelatedField(
        view_name='openstacktenant-flavor-detail',
        queryset=openstack_tenant_models.Flavor.objects.all(),
        lookup_field='uuid',
        allow_null=True,
        write_only=True,
        required=False,
    )
    storage = serializers.IntegerField(write_only=True)
    memory = serializers.IntegerField(write_only=True, required=False)
    cpu = serializers.IntegerField(write_only=True, required=False)
    roles = serializers.MultipleChoiceField(choices=['controlplane', 'etcd', 'worker'], write_only=True)

    class Meta(object):
        model = models.Node


class NestedNodeSerializer(BaseNodeSerializer):
    instance = core_serializers.GenericRelatedField(
        related_models=VirtualMachine.get_all_models(),
        read_only=True
    )

    class Meta(BaseNodeSerializer.Meta):
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
        name = attrs.get('name')
        spl = attrs.get('service_project_link')
        utils.expand_added_nodes(nodes, spl, name)
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


class CreateNodeSerializer(BaseNodeSerializer):
    class Meta:
        model = models.Node
        fields = ('cluster', 'roles', 'storage', 'memory', 'cpu', 'subnet', 'flavor')
        extra_kwargs = {
            'cluster': {'lookup_field': 'uuid', 'view_name': 'rancher-cluster-detail'}
        }

    def validate(self, attrs):
        cluster = attrs.get('cluster')
        spl = cluster.service_project_link
        node = attrs
        utils.expand_added_nodes([node], spl, cluster.name)
        return super(CreateNodeSerializer, self).validate(attrs)


class ClusterImportableSerializer(serializers.Serializer):
    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='rancher-spl-detail',
        queryset=models.RancherServiceProjectLink.objects.all(),
        write_only=True)

    name = serializers.CharField(read_only=True)
    backend_id = serializers.CharField(source="id", read_only=True)
    type = serializers.SerializerMethodField()
    extra = serializers.SerializerMethodField()

    def get_type(self, cluster):
        return 'Rancher.Cluster'

    def get_extra(self, cluster):
        spec = cluster.get('appliedSpec', {})
        config = spec.get('rancherKubernetesEngineConfig', {})
        backend_nodes = config.get('nodes', [])
        return [
            {
                'name': 'Number of nodes',
                'value': len(backend_nodes),
            },
            {
                'name': 'Created at',
                'value': cluster.get('created'),
            },
        ]


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
